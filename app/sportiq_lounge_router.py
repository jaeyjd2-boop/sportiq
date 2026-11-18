import html as _html_lib
import json
import os
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Set, Tuple

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from app.sqlite_guard import (
    DEFAULT_BUSY_TIMEOUT_MS,
    DEFAULT_TIMEOUT_SEC,
    install_sqlite_guard,
    managed_sqlite_conn,
    run_with_db_retry,
)
from app.sportiq_member_upgrade import inject_upgrade_into_html, upgrade_router
import app.sportiq_member_upgrade as _smu

_SORT_DEDUP_APPLIED = False

_FIXED_SORT_STRIP_FN = r"""function isElementHiddenOrCollapsed(el) {
    if (!el) return true;
    if (el.id === "proTickerList" || el.id === "dcTickerControls") return true;
    if (el.style && el.style.display === "none") return true;
    if (el.classList && (el.classList.contains("hidden") || el.classList.contains("sq-match-sort-bar"))) return true;
    if (typeof window.getComputedStyle === "function") {
      try {
        var cs = window.getComputedStyle(el);
        if (cs && (cs.display === "none" || cs.visibility === "hidden")) return true;
      } catch (e) {}
    }
    if (typeof el.getClientRects === "function" && el.getClientRects().length === 0) return true;
    return false;
  }

  function countDirectMatchCardChildren(el) {
    if (!el || !el.children || el.children.length < 2 || el.children.length > 500) return 0;
    if (
      el.id === "proLiveTickerBar" ||
      el.id === "proTickerList" ||
      el.id === "dcTickerControls" ||
      el.id === "feedList" ||
      el.id === "dcLauncherGrid" ||
      el.id === "globalTop10Grid"
    ) {
      return 0;
    }
    if (isElementHiddenOrCollapsed(el)) return 0;

    var rawKids = Array.prototype.slice.call(el.children);
    var cardKids = [];
    for (var i = 0; i < rawKids.length; i++) {
      var ch = rawKids[i];
      if (isElementHiddenOrCollapsed(ch)) continue;
      cardKids.push(ch);
    }
    if (cardKids.length < 2) return 0;

    var matchLikeCount = 0;
    for (var j = 0; j < cardKids.length; j++) {
      var c = cardKids[j];
      var t = c.textContent || "";
      if (
        (parseMatchEpochFromText(t) != null || t.indexOf("최근 시즌 기록") !== -1) &&
        /Scheduled|예정|종료|Final|AET|LIVE|배당|스코어|vs/i.test(t)
      ) {
        matchLikeCount++;
      }
    }
    if (matchLikeCount < 2 || matchLikeCount < Math.ceil(cardKids.length * 0.4)) return 0;
    return matchLikeCount;
  }

  function hasDescendantMatchStrip(container) {
    if (!container || !container.querySelectorAll) return false;
    if (container.id !== "dcLiveTickerTrack" && container.querySelector("#dcLiveTickerTrack")) {
      return true;
    }
    var subEls = container.querySelectorAll("div, section, ul");
    for (var i = 0; i < subEls.length; i++) {
      if (subEls[i] !== container && countDirectMatchCardChildren(subEls[i]) >= 2) {
        return true;
      }
    }
    return false;
  }

  function cleanupDuplicateMatchSortBars() {
    var tickerWrap = document.getElementById("proLiveTickerBar");
    if (tickerWrap) {
      var prev = tickerWrap.previousElementSibling;
      while (prev && prev.classList && prev.classList.contains("sq-match-sort-bar")) {
        var toDrop = prev;
        prev = prev.previousElementSibling;
        if (toDrop.parentNode) toDrop.parentNode.removeChild(toDrop);
      }
      if (tickerWrap.__sqToolbarEl) tickerWrap.__sqToolbarEl = null;

      var liveTrack = document.getElementById("dcLiveTickerTrack");
      var innerBars = tickerWrap.querySelectorAll(".sq-match-sort-bar");
      if (liveTrack && innerBars.length > 0) {
        var keeper = liveTrack.__sqToolbarEl || innerBars[0];
        for (var k = 0; k < innerBars.length; k++) {
          if (innerBars[k] !== keeper && innerBars[k].parentNode) {
            innerBars[k].parentNode.removeChild(innerBars[k]);
          }
        }
        if (keeper && keeper.nextElementSibling !== liveTrack && liveTrack.parentNode) {
          liveTrack.parentNode.insertBefore(keeper, liveTrack);
        }
        liveTrack.__sqToolbarEl = keeper;
      }
    }
    var primaryTrack = document.getElementById("dcLiveTickerTrack");
    if (primaryTrack) {
      var allBars = document.querySelectorAll(".sq-match-sort-bar");
      var mainBar = primaryTrack.__sqToolbarEl || null;
      for (var m = 0; m < allBars.length; m++) {
        if (!mainBar && primaryTrack.parentNode && allBars[m].parentNode === primaryTrack.parentNode) {
          mainBar = allBars[m];
        }
      }
      for (var x = 0; x < allBars.length; x++) {
        if (mainBar && allBars[x] !== mainBar && allBars[x].parentNode) {
          allBars[x].parentNode.removeChild(allBars[x]);
        }
      }
    }
  }

  function sortRenderedMatchCardStrips(forceResort) {
    cleanupDuplicateMatchSortBars();

    document.querySelectorAll("div, section, ul").forEach(function (container) {
      if (countDirectMatchCardChildren(container) < 2) return;
      if (hasDescendantMatchStrip(container)) return;

      var kids = Array.prototype.slice.call(container.children).filter(function (ch) {
        return !isElementHiddenOrCollapsed(ch);
      });
      if (kids.length < 2) return;

      bindSmoothHorizontalControls(container);
      if (container.id === "dcLiveTickerTrack" || !document.getElementById("dcLiveTickerTrack")) {
        mountSortToggleAboveStrip(container);
      }

      var scroller = resolveActualScrollViewport(container);
      if (container.__sqToolbarEl && scroller) {
        updateNavButtonEdgeStates(container.__sqToolbarEl, scroller);
      }

      if (!forceResort && isStripUserInteracting(container)) return;

      var firstTxt = (kids[0].textContent || "").slice(0, 48);
      var lastTxt = (kids[kids.length - 1].textContent || "").slice(0, 48);
      var sig = matchSortMode + "|" + kids.length + "|" + firstTxt + "|" + lastTxt;
      if (!forceResort && container.__sqSortedSig === sig) return;

      var decorated = kids.map(function (el, idx) {
        var t = el.textContent || "";
        var isSeedArch = el.getAttribute("data-sq-seed-archive") === "1" || t.indexOf("최근 시즌 기록") !== -1;
        var ep = isSeedArch ? null : parseMatchEpochFromText(t);
        var ord = isSeedArch ? { bucket: 8, key: idx } : computeMatchOrderTuple(t, ep, idx);
        var isMy = /★\s*MY\b/.test(t) ? 0 : 1;
        return { el: el, idx: idx, isMy: isMy, bucket: ord.bucket, key: ord.key };
      });

      var sorted = decorated.slice().sort(function (a, b) {
        if (a.bucket !== b.bucket) return a.bucket - b.bucket;
        if (a.key !== b.key) return a.key - b.key;
        if (a.isMy !== b.isMy) return a.isMy - b.isMy;
        return a.idx - b.idx;
      });

      var changed = Boolean(forceResort);
      if (!changed) {
        for (var i = 0; i < sorted.length; i++) {
          if (sorted[i].el !== kids[i]) { changed = true; break; }
        }
      }
      if (changed) {
        var prevScrollLeft = scroller ? (scroller.scrollLeft || 0) : (container.scrollLeft || 0);
        sorted.forEach(function (item) { container.appendChild(item.el); });
        if (scroller) scroller.scrollLeft = forceResort ? 0 : prevScrollLeft;
      }
      var newKids = Array.prototype.slice.call(container.children).filter(function (ch) {
        return !isElementHiddenOrCollapsed(ch);
      });
      container.__sqSortedSig = matchSortMode + "|" + newKids.length + "|" +
        ((newKids[0] && newKids[0].textContent) || "").slice(0, 48) + "|" +
        ((newKids[newKids.length - 1] && newKids[newKids.length - 1].textContent) || "").slice(0, 48);
      if (container.__sqToolbarEl && scroller) {
        updateNavButtonEdgeStates(container.__sqToolbarEl, scroller);
      }
    });

    cleanupDuplicateMatchSortBars();
  }"""


_ENHANCED_RENDER_AI_IQ_FN = r'''function renderAiIqSearchResult(d) {
    var box = document.getElementById("sqAiIqResultArea");
    if (!box) return;
    var summaryHtml = esc(d.ai_summary || "").replace(/\n/g, "<br/>");
    var expanded = Array.isArray(d.expanded_terms) ? d.expanded_terms : [];
    var know = d.knowledge || [];
    var games = d.sportiq_matches || [];
    var insights = d.sportiq_insights || [];
    var posts = d.sportiq_posts || [];
    var news = d.web_news || [];
    var totalCnt = Number(d.total_count || (know.length + games.length + insights.length + posts.length + news.length));

    var out = [
      "<div style='background:linear-gradient(145deg,rgba(6,182,212,.15),rgba(15,23,42,.97));border:1px solid rgba(34,211,238,.48);border-radius:14px;padding:14px 15px;margin-bottom:10px;'>",
      "  <div style='display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:6px;'>",
      "    <div style='display:flex;align-items:center;gap:7px;flex-wrap:wrap;'>",
      "      <span style='font-weight:900;color:#67e8f9;font-size:13.5px;'>🧠 AI 아이큐 초정밀 통합 브리핑</span>",
      "      <span style='background:rgba(16,185,129,.18);border:1px solid rgba(52,211,153,.45);color:#6ee7b7;border-radius:999px;padding:1px 8px;font-size:11px;font-weight:800;'>총 " + esc(totalCnt) + "건 정밀 매칭</span>",
      "    </div>",
      "    <span style='font-size:11px;color:#94a3b8;'>" + esc(d.searched_at_kst || "") + "</span>",
      "  </div>",
      (expanded.length > 1
        ? "<div style='font-size:11px;color:#93c5fd;background:rgba(2,6,23,.55);border-radius:8px;padding:4px 9px;margin-bottom:8px;'>🌐 한·영 동의어 자동 확장: <strong>" + expanded.slice(0, 6).map(esc).join(" · ") + "</strong></div>"
        : ""),
      "  <div style='font-size:13px;color:#f1f5f9;line-height:1.68;'>" + summaryHtml + "</div>",
      "  <div style='display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;margin-top:11px;padding-top:9px;border-top:1px dashed rgba(148,163,184,.2);'>",
      "    <div style='display:flex;gap:5px;flex-wrap:wrap;'>",
      (d.related_queries || []).map(function (rq) {
        return "<button type='button' data-ai-rel='" + esc(rq) + "' style='background:#0f172a;border:1px solid #334155;color:#93c5fd;border-radius:999px;padding:2px 9px;font-size:11px;cursor:pointer;'>🔎 " + esc(rq) + "</button>";
      }).join(""),
      "    </div>",
      "    <button type='button' id='sqAiWriteFromSearchBtn' style='background:#0284c7;color:#fff;border:0;border-radius:8px;padding:5px 11px;font-size:11.5px;font-weight:800;cursor:pointer;'>✍️ 이 주제로 분석글 쓰기 (+15P)</button>",
      "  </div>",
      "</div>"
    ];

    // Category Filter Bar
    var catTabs = [
      ["all", "전체 (" + totalCnt + ")"],
      ["matches", "⚡ 경기·배당 (" + games.length + ")"],
      ["insights", "📌 순위·부상·이적 (" + insights.length + ")"],
      ["posts", "✍️ 라운지 분석 (" + posts.length + ")"],
      ["news", "📰 국내외 뉴스 (" + news.length + ")"],
      ["know", "📚 백과 (" + know.length + ")"]
    ];
    out.push("<div id='sqAiCatBar' style='display:flex;gap:5px;flex-wrap:wrap;margin:8px 0 12px;'>");
    catTabs.forEach(function (ct) {
      var cntVal = ct[0] === "all" ? totalCnt : (ct[0] === "matches" ? games.length : (ct[0] === "insights" ? insights.length : (ct[0] === "posts" ? posts.length : (ct[0] === "news" ? news.length : know.length))));
      if (ct[0] !== "all" && cntVal === 0) return;
      var activeStyle = ct[0] === "all"
        ? "background:#0284c7;color:#fff;border-color:#38bdf8;"
        : "background:#0f172a;color:#cbd5e1;border-color:#334155;";
      out.push("<button type='button' data-ai-cat='" + ct[0] + "' style='border:1px solid;border-radius:999px;padding:4px 10px;font-size:11.5px;font-weight:800;cursor:pointer;" + activeStyle + "'>" + ct[1] + "</button>");
    });
    out.push("</div>");

    function pctBadge(pct) {
      var v = Number(pct || 0);
      if (!v) return "";
      var col = v >= 90 ? "#34d399" : (v >= 80 ? "#38bdf8" : "#cbd5e1");
      return "<span style='font-size:10.5px;font-weight:800;color:" + col + ";background:rgba(15,23,42,.9);border:1px solid rgba(148,163,184,.25);border-radius:6px;padding:1px 6px;'>정확도 " + v + "%</span>";
    }

    // 1. SportIQ Live Match & Odds Cards (Clickable -> Scrolls to Live Score / Schedule)
    if (games.length) {
      out.push("<div data-ai-sec='matches'><div style='font-weight:800;color:#4ade80;font-size:12.5px;margin:10px 0 6px;'>📊 SportIQ 실시간 매치 · AI 승률 · 배당 (" + games.length + "건)</div>");
      games.forEach(function (g) {
        out.push(
          "<div class='sq-ai-iq-card' data-ai-jump-match='" + esc(g.matchup || "") + "' style='display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;cursor:pointer;' title='클릭 시 실시간 경기 화면으로 이동'>" +
          "  <div>" +
          "    <span style='background:#1e293b;color:#38bdf8;border-radius:6px;padding:2px 6px;font-size:10.5px;font-weight:800;margin-right:6px;'>" + esc(g.league) + "</span>" +
          "    <strong style='color:#f8fafc;font-size:13px;'>" + esc(g.matchup) + "</strong> " + pctBadge(g.match_pct) +
          "    <div style='font-size:11.5px;color:#94a3b8;margin-top:3px;'>📅 " + esc(g.dt_kst) + " · 스코어 <strong style='color:#e2e8f0;'>" + esc(g.score || "0 : 0") + "</strong> · 🎯 " + esc(g.ai_prob) + (g.drop_badge ? " · <span style='color:#fbbf24;font-weight:800;'>" + esc(g.drop_badge) + "</span>" : "") + "</div>" +
          "  </div>" +
          "  <span style='font-size:11.5px;font-weight:800;color:#e2e8f0;background:#020617;border:1px solid #334155;border-radius:8px;padding:4px 9px;'>" + esc(g.status) + " ↗</span>" +
          "</div>"
        );
      });
      out.push("</div>");
    }

    // 2. SportIQ Standings, Injuries & Transfer Insights (NEW!)
    if (insights.length) {
      out.push("<div data-ai-sec='insights'><div style='font-weight:800;color:#c084fc;font-size:12.5px;margin:10px 0 6px;'>📌 구단 순위 · 부상결장 · 이적시장 핵심 데이터 (" + insights.length + "건)</div>");
      insights.forEach(function (ins) {
        var tagOpen = ins.url
          ? "<a href='" + esc(ins.url) + "' target='_blank' rel='noopener noreferrer' class='sq-ai-iq-card' style='display:block;text-decoration:none;'>"
          : "<div class='sq-ai-iq-card' data-ai-jump-dc='" + esc(ins.dc_tab || "standings") + "' style='cursor:pointer;'>";
        var tagClose = ins.url ? "</a>" : "</div>";
        out.push(
          tagOpen +
          "  <div style='display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:3px;'>" +
          "    <span style='font-size:11px;font-weight:800;color:#e9d5ff;background:rgba(147,51,234,.22);border:1px solid rgba(192,132,252,.35);border-radius:6px;padding:1px 7px;'>" + esc(ins.badge || "데이터센터") + "</span>" +
          "    " + pctBadge(ins.match_pct) +
          "  </div>" +
          "  <div style='font-weight:800;color:#f8fafc;font-size:13px;'>" + esc(ins.title) + "</div>" +
          "  <div style='font-size:11.5px;color:#cbd5e1;margin-top:3px;'>" + esc(ins.detail) + "</div>" +
          tagClose
        );
      });
      out.push("</div>");
    }

    // 3. SportIQ Community & AI Auto-Analyst Posts (Previously unrendered — NOW LIVE!)
    if (posts.length) {
      out.push("<div data-ai-sec='posts'><div style='font-weight:800;color:#fbbf24;font-size:12.5px;margin:10px 0 6px;'>✍️ SportIQ 라운지 &amp; AI 분석 브리핑 (" + posts.length + "건)</div>");
      posts.forEach(function (p) {
        out.push(
          "<div class='sq-ai-iq-card' data-ai-jump-feed='" + esc(d.query || "") + "' style='cursor:pointer;' title='클릭 시 분석 피드에서 보기'>" +
          "  <div style='display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:4px;'>" +
          "    <span><span style='background:#1e293b;color:#fde68a;border-radius:6px;padding:1px 6px;font-size:10.5px;font-weight:800;margin-right:5px;'>" + esc(p.league || "분석") + "</span><span style='font-size:11px;color:#94a3b8;'>✍️ " + esc(p.author || "SportIQ 분석가") + "</span></span>" +
          "    " + pctBadge(p.match_pct) +
          "  </div>" +
          "  <div style='font-weight:800;color:#f8fafc;font-size:13px;'>" + esc(p.title) + "</div>" +
          (p.excerpt ? "<div style='font-size:11.5px;color:#94a3b8;margin-top:3px;'>" + esc(p.excerpt) + "</div>" : "") +
          "</div>"
        );
      });
      out.push("</div>");
    }

    // 4. Knowledge / Encyclopedia Cards
    if (know.length) {
      out.push("<div data-ai-sec='know'><div style='font-weight:800;color:#fde68a;font-size:12.5px;margin:10px 0 6px;'>📚 백과 · 구단/선수 배경지식 (" + know.length + "건)</div>");
      know.forEach(function (k) {
        out.push(
          "<div class='sq-ai-iq-card'>" +
          "  <div style='display:flex;justify-content:space-between;align-items:center;gap:8px;'>" +
          "    <strong style='color:#f8fafc;font-size:13px;'>" + esc(k.title) + " <span style='font-size:11px;color:#38bdf8;font-weight:700;'>(" + esc(k.source) + ")</span></strong>" +
          "    <a href='" + esc(k.url) + "' target='_blank' rel='noopener noreferrer' style='font-size:11px;color:#34d399;font-weight:800;text-decoration:none;'>원문 보기 ↗</a>" +
          "  </div>" +
          "  <div style='font-size:12px;color:#cbd5e1;margin-top:5px;'>" + esc(k.summary) + "</div>" +
          "</div>"
        );
      });
      out.push("</div>");
    }

    // 5. High-Precision Domestic & Global News List (Up to 28 items with Expand Toggle!)
    if (news.length) {
      var initialCap = 12;
      out.push("<div data-ai-sec='news'><div style='font-weight:800;color:#38bdf8;font-size:12.5px;margin:10px 0 6px;'>🌐 정확도순 국내·해외 실시간 스포츠 뉴스 (" + news.length + "건)</div>");
      news.forEach(function (n, idx) {
        var extraAttr = idx >= initialCap ? " data-ai-extra-news='1' style='display:none;text-decoration:none;'" : " style='display:block;text-decoration:none;'";
        out.push(
          "<a href='" + esc(n.url) + "' target='_blank' rel='noopener noreferrer' class='sq-ai-iq-card'" + extraAttr + ">" +
          "  <div style='display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:4px;flex-wrap:wrap;'>" +
          "    <span style='font-size:11px;font-weight:800;color:#22d3ee;'>📰 " + esc(n.source) + (n.lang_badge ? " <span style='color:#a5b4fc;'>[" + esc(n.lang_badge) + "]</span>" : "") + "</span>" +
          "    <span style='display:inline-flex;align-items:center;gap:6px;'>" + pctBadge(n.match_pct) + "<span style='font-size:11px;color:#94a3b8;'>🕒 " + esc(n.published_kst) + " ↗</span></span>" +
          "  </div>" +
          "  <div style='font-weight:800;color:#f8fafc;font-size:13px;'>" + esc(n.title) + "</div>" +
          (n.snippet ? "<div style='font-size:11.5px;color:#94a3b8;margin-top:3px;'>" + esc(n.snippet) + "</div>" : "") +
          "</a>"
        );
      });
      if (news.length > initialCap) {
        out.push(
          "<button type='button' id='sqAiMoreNewsBtn' style='width:100%;margin-top:6px;background:#0f172a;border:1px solid #334155;color:#7dd3fc;border-radius:10px;padding:8px;font-size:12px;font-weight:800;cursor:pointer;'>➕ 정확도 검증 뉴스 나머지 " + (news.length - initialCap) + "건 더 펼쳐보기 ▼</button>"
        );
      }
      out.push("</div>");
    }

    box.innerHTML = out.join("");

    // Category filter click handler
    box.querySelectorAll("[data-ai-cat]").forEach(function (cb) {
      cb.onclick = function () {
        var targetCat = cb.getAttribute("data-ai-cat") || "all";
        box.querySelectorAll("[data-ai-cat]").forEach(function (b) {
          var on = b === cb;
          b.style.background = on ? "#0284c7" : "#0f172a";
          b.style.color = on ? "#fff" : "#cbd5e1";
          b.style.borderColor = on ? "#38bdf8" : "#334155";
        });
        box.querySelectorAll("[data-ai-sec]").forEach(function (sec) {
          var sc = sec.getAttribute("data-ai-sec");
          sec.style.display = (targetCat === "all" || sc === targetCat) ? "block" : "none";
        });
      };
    });

    // Expand extra news button
    var moreBtn = document.getElementById("sqAiMoreNewsBtn");
    if (moreBtn) {
      moreBtn.onclick = function () {
        box.querySelectorAll("[data-ai-extra-news='1']").forEach(function (el) {
          el.style.display = "block";
        });
        moreBtn.style.display = "none";
      };
    }

    // Jump handlers for match, datacenter insight, and feed post cards
    box.querySelectorAll("[data-ai-jump-match]").forEach(function (mc) {
      mc.onclick = function () {
        var modal = document.getElementById("sqAiIqModal");
        if (modal) modal.style.display = "none";
        var liveBar = document.getElementById("proLiveTickerBar");
        if (liveBar && liveBar.scrollIntoView) liveBar.scrollIntoView({ behavior: "smooth", block: "center" });
      };
    });
    box.querySelectorAll("[data-ai-jump-dc]").forEach(function (dcEl) {
      dcEl.onclick = function () {
        var tabName = dcEl.getAttribute("data-ai-jump-dc") || "standings";
        var modal = document.getElementById("sqAiIqModal");
        if (modal) modal.style.display = "none";
        var hubBody = document.getElementById("dcHubBody");
        if (hubBody) hubBody.style.display = "block";
        var card = document.querySelector("[data-tab='" + tabName + "']");
        if (card) card.click();
        var dcWrap = document.getElementById("dcHubCollapsible") || hubBody;
        if (dcWrap && dcWrap.scrollIntoView) dcWrap.scrollIntoView({ behavior: "smooth", block: "start" });
      };
    });
    box.querySelectorAll("[data-ai-jump-feed]").forEach(function (fp) {
      fp.onclick = function () {
        var qTxt = fp.getAttribute("data-ai-jump-feed") || "";
        var modal = document.getElementById("sqAiIqModal");
        if (modal) modal.style.display = "none";
        if (typeof window.switchTab === "function") window.switchTab("feed");
        var searchInp = document.querySelector("input[placeholder*='검색']");
        if (searchInp && qTxt) {
          searchInp.value = qTxt.split(/\s+/)[0];
          searchInp.dispatchEvent(new Event("input", { bubbles: true }));
        }
        var fl = document.getElementById("feedList");
        if (fl && fl.scrollIntoView) fl.scrollIntoView({ behavior: "smooth", block: "start" });
      };
    });

    box.querySelectorAll("[data-ai-rel]").forEach(function (rb) {
      rb.onclick = function () {
        var rq = rb.getAttribute("data-ai-rel") || "";
        var inp = document.getElementById("sqAiIqInput");
        if (inp) inp.value = rq;
        executeAiIqSearch(rq);
      };
    });

    var wBtn = document.getElementById("sqAiWriteFromSearchBtn");
    if (wBtn) {
      wBtn.onclick = function () {
        var modal = document.getElementById("sqAiIqModal");
        if (modal) modal.style.display = "none";
        var tEl = document.getElementById("postTitle");
        var bEl = document.getElementById("postContent");
        if (tEl) tEl.value = "[AI 아이큐 리서치] " + (d.query || "") + " 핵심 분석";
        if (bEl) {
          var topLinks = (d.web_news || []).slice(0, 3).map(function (x) { return "- " + x.title + " (" + x.source + ")"; }).join("\n");
          bEl.value = (d.ai_summary || "") + (topLinks ? "\n\n[참고 온라인 뉴스]\n" + topLinks : "") + "\n\n[내 승부예측 의견]\n";
        }
        if (typeof window.openModal === "function") window.openModal("writeModal");
        else {
          var wm = document.getElementById("writeModal");
          if (wm) wm.classList.remove("hidden");
        }
      };
    }
  }'''

_AI_SEARCH_UI_PATCHED = False




_FRONTEND_V22_JS = r'''
/* ==========================================================================
   SportIQ Member UX Polish v2.2
   1) Server DB Likes & Bookmarks + Unified +20P Check-in + Tier Gauge Card + Live Member Ranking
   2) 1-Row Main Community Tab Bar + Dynamic Match Count + Official Non-Betting Legal/Trust Footer
   3) Overseas Sports News Korean Summary Badges + NFL / UFC / F1 Filter Buttons
   ========================================================================== */
(function () {
  if (window.__SPORTIQ_MEMBER_UX_V22__) return;
  window.__SPORTIQ_MEMBER_UX_V22__ = true;

  var LS_NICK = "sportiq_sync_nick";
  var LS_PIN = "sportiq_sync_pin";
  var LS_ACTOR = "sportiq_actor_uid_v22";
  var bookmarkOnlyFilter = false;
  var reactionCache = { like_counts: {}, liked_ids: {}, bookmarked_ids: {} };
  var overseasCacheV22 = [];
  var overseasCountsV22 = {};
  var overseasActiveSportV22 = "전체";

  function uxEsc(v) {
    if (v === null || v === undefined) return "";
    return String(v)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function getSavedNickV22() {
    try { return (localStorage.getItem(LS_NICK) || "").trim(); } catch (e) { return ""; }
  }

  function getSavedPinV22() {
    try { return (localStorage.getItem(LS_PIN) || "").replace(/\D/g, ""); } catch (e) { return ""; }
  }

  function getActorKeyV22() {
    var nk = getSavedNickV22();
    if (nk && nk.length >= 2) return "member:" + nk.toLowerCase();
    try {
      var uid = localStorage.getItem(LS_ACTOR);
      if (!uid) {
        uid = "anon:" + Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
        localStorage.setItem(LS_ACTOR, uid);
      }
      return uid;
    } catch (e) {
      return "anon:session";
    }
  }

  function getTodayKstStr() {
    try {
      return new Intl.DateTimeFormat("sv-SE", { timeZone: "Asia/Seoul" }).format(new Date());
    } catch (e) {
      return new Date().toISOString().slice(0, 10);
    }
  }

  // -------------------------------------------------------------------------
  // 0. Inject Clean CSS for 1-Line Tab Bar, Tier Gauge, Overseas Badges & Footer
  // -------------------------------------------------------------------------
  function injectUxV22Styles() {
    if (document.getElementById("sqUxV22Styles")) return;
    var st = document.createElement("style");
    st.id = "sqUxV22Styles";
    st.textContent = [
      "/* 1-Line Main Community Tab Bar (Zero 2nd-row orphan wrap) */",
      ".sq-main-tabbar-row {",
      "  display: flex !important;",
      "  flex-wrap: nowrap !important;",
      "  align-items: center !important;",
      "  gap: 6px !important;",
      "  overflow-x: auto !important;",
      "  -webkit-overflow-scrolling: touch;",
      "  scrollbar-width: none !important;",
      "}",
      ".sq-main-tabbar-row::-webkit-scrollbar { display: none !important; }",
      ".sq-main-tabbar-row > .tab-btn,",
      ".sq-main-tabbar-row > button {",
      "  flex: 0 0 auto !important;",
      "  white-space: nowrap !important;",
      "  padding: 7px 11.5px !important;",
      "  font-size: 12.8px !important;",
      "  line-height: 1.25 !important;",
      "}",
      "@media (min-width: 1200px) {",
      "  .sq-main-tabbar-row { gap: 7px !important; }",
      "  .sq-main-tabbar-row > .tab-btn,",
      "  .sq-main-tabbar-row > button { padding: 8px 13px !important; font-size: 13.2px !important; }",
      "}",
      "/* Overseas KO Summary Card */",
      ".sq-rss-card-v22 {",
      "  display: block;",
      "  text-decoration: none;",
      "  background: rgba(15, 23, 42, 0.82);",
      "  border: 1px solid rgba(51, 65, 85, 0.78);",
      "  border-radius: 14px;",
      "  padding: 12px 14px;",
      "  transition: border-color 0.16s ease, transform 0.16s ease, background 0.16s ease;",
      "}",
      ".sq-rss-card-v22:hover {",
      "  border-color: rgba(56, 189, 248, 0.65);",
      "  background: rgba(15, 23, 42, 0.98);",
      "  transform: translateY(-1px);",
      "}",
      "/* Official Non-Betting Legal & Trust Footer */",
      "#sqOfficialLegalFooter {",
      "  margin-top: 40px;",
      "  border-top: 1px solid rgba(51, 65, 85, 0.85);",
      "  background: linear-gradient(180deg, rgba(9, 14, 28, 0.96) 0%, #020617 100%);",
      "  color: #94a3b8;",
      "  padding: 28px 16px 104px;",
      "  font-size: 12px;",
      "  line-height: 1.65;",
      "}",
      ".sq-footer-link-btn {",
      "  background: rgba(30, 41, 59, 0.7);",
      "  border: 1px solid rgba(71, 85, 105, 0.65);",
      "  color: #e2e8f0;",
      "  border-radius: 999px;",
      "  padding: 5px 12px;",
      "  font-size: 11.5px;",
      "  font-weight: 700;",
      "  cursor: pointer;",
      "  transition: all 0.15s ease;",
      "}",
      ".sq-footer-link-btn:hover {",
      "  border-color: #34d399;",
      "  color: #6ee7b7;",
      "  background: rgba(16, 185, 129, 0.12);",
      "}"
    ].join("\n");
    document.head.appendChild(st);
  }

  // -------------------------------------------------------------------------
  // 1. 1-Row Main Tab Bar + Dynamic Match Count ('총 20경기' -> actual count)
  // -------------------------------------------------------------------------
  function enforceSingleLineMainTabBar() {
    var firstTab = document.querySelector("button.tab-btn[data-tab='feed'], button[onclick*=\"switchTab('feed')\"]");
    if (firstTab && firstTab.parentElement) {
      var bar = firstTab.parentElement;
      bar.classList.add("sq-main-tabbar-row");
      bar.style.flexWrap = "nowrap";
      bar.style.overflowX = "auto";
    }
  }

  function syncTop20MatchCountBadge() {
    var sec = document.getElementById("tab-matches");
    if (!sec) return;
    var actualCount = 0;
    var buttons = sec.querySelectorAll("button");
    for (var i = 0; i < buttons.length; i++) {
      var bt = buttons[i].textContent || "";
      var m = bt.match(/전체\s*TOP\s*20\s*\((\d+)\)/i);
      if (m) {
        actualCount = parseInt(m[1], 10);
        break;
      }
    }
    if (!actualCount) {
      var topGrid = document.getElementById("globalTop10Grid");
      if (topGrid && topGrid.children && topGrid.children.length > 0) {
        actualCount = topGrid.children.length;
      }
    }
    if (!actualCount) return;

    var spans = sec.querySelectorAll("span, div");
    for (var j = 0; j < spans.length; j++) {
      var el = spans[j];
      if (el.children && el.children.length > 0) continue;
      var txt = el.textContent || "";
      if (txt.indexOf("화제성 TOP 20 연동") !== -1 && /총\s*\d+\s*경기/.test(txt)) {
        var nextTxt = txt.replace(/총\s*\d+\s*경기/, "총 " + actualCount + "경기");
        if (nextTxt !== txt) el.textContent = nextTxt;
      }
    }
  }

  // -------------------------------------------------------------------------
  // 2. Unified +20P Attendance Check-in & Real-Time Tier Upgrade Gauge Card
  // -------------------------------------------------------------------------
  function renderTierUpgradeSidebarCard() {
    var pts = (typeof state === "object" && state) ? Number(state.points || 120) : 120;
    var role = pts >= 350 ? "PRO" : (pts >= 150 ? "MEMBER" : "GUEST");
    var nk = getSavedNickV22();

    // Update top header role buttons clarity
    document.querySelectorAll("button[onclick*=\"setRole(\"]").forEach(function (btn) {
      var oc = btn.getAttribute("onclick") || "";
      if (oc.indexOf("'MEMBER'") !== -1) {
        btn.title = "150P 달성 시 정회원(MEMBER) 자동 승급";
        if (pts < 150 && (btn.textContent || "").indexOf("150P") === -1) {
          btn.textContent = "정회원 150P+";
        } else if (pts >= 150) {
          btn.textContent = "정회원 MEMBER";
        }
      } else if (oc.indexOf("'PRO'") !== -1) {
        btn.title = "350P 달성 시 PRO 분석가 자동 승급";
        if (pts < 350 && (btn.textContent || "").indexOf("350P") === -1) {
          btn.textContent = "PRO 350P+";
        } else if (pts >= 350) {
          btn.textContent = "PRO 분석가";
        }
      }
    });

    // Update Attendance Card (+10P -> +20P unified)
    var attBtn = document.getElementById("attendanceBtn");
    var mobAttBtn = document.getElementById("mobileAttendanceBtn");
    var todayStr = getTodayKstStr();
    var checkedToday = false;
    try {
      checkedToday = Boolean((typeof state === "object" && state && state.checkedToday) || localStorage.getItem("sportiq_checked") === todayStr);
    } catch (e) {}
    var streakVal = (typeof state === "object" && state && state.streak != null) ? Number(state.streak) : 0;
    var streakEl = document.getElementById("streakDays");
    if (streakEl) streakEl.textContent = String(streakVal);

    if (attBtn) {
      var attCard = attBtn.parentElement;
      if (attCard) {
        attCard.querySelectorAll("p, div, span").forEach(function (node) {
          if (node.children && node.children.length > 0) return;
          var t = node.textContent || "";
          if (t.indexOf("+10P") !== -1 && node !== attBtn) {
            node.textContent = "매일 출석 시 +20P 적립 (7일 연속 +30P · 워드프레스 통합)";
          }
        });
      }
      attBtn.textContent = checkedToday
        ? ("🎉 오늘 출석 완료 (연속 " + Math.max(1, streakVal) + "일째 · +20P)")
        : "✅ 출석 체크하고 +20P 받기";
    }
    if (mobAttBtn && (mobAttBtn.textContent || "").indexOf("+10P") !== -1) {
      mobAttBtn.textContent = mobAttBtn.textContent.replace("+10P", "+20P");
    }

    // Replace conflicting '1초 무료 전환' card (#sidebarMemberBtn parent) with Live Tier Progress Gauge
    var oldBtn = document.getElementById("sidebarMemberBtn");
    var hostCard = document.getElementById("sqMemberTierGaugeCard") || (oldBtn ? oldBtn.parentElement : null);
    if (!hostCard) return;
    hostCard.id = "sqMemberTierGaugeCard";

    var targetPts = pts < 150 ? 150 : 350;
    var remainPts = Math.max(0, targetPts - pts);
    var pct = pts >= 350 ? 100 : (pts < 150 ? Math.min(100, Math.round((pts / 150) * 100)) : Math.min(100, Math.round(((pts - 150) / 200) * 100)));
    var tierLabel = role === "PRO" ? "👑 PRO 분석가" : (role === "MEMBER" ? "✅ 정회원 (MEMBER)" : "🌱 오픈 게스트");
    var nextMsg = role === "PRO"
      ? "최고 등급 PRO 분석가 권한이 활성화되어 있습니다!"
      : (role === "MEMBER"
          ? ("PRO 분석가(350P) 자동 승급까지 " + remainPts + "P 남았습니다.")
          : ("정회원(150P) 자동 승급까지 " + remainPts + "P 남았습니다! (출석 +20P · 글작성 +15P)"));

    hostCard.innerHTML = [
      "<div style='display:flex;justify-content:space-between;align-items:center;gap:6px;'>",
      "  <span style='font-size:11px;font-weight:900;color:#34d399;letter-spacing:0.04em;'>🏆 실시간 등급 · 승급 게이지</span>",
      "  <span style='background:rgba(16,185,129,.16);border:1px solid rgba(52,211,153,.4);color:#6ee7b7;border-radius:999px;padding:2px 8px;font-size:11px;font-weight:800;'>" + uxEsc(tierLabel) + " · " + pts + "P</span>",
      "</div>",
      "<div style='font-size:13.5px;font-weight:900;color:#f8fafc;margin-top:4px;'>150P 정회원 · 350P PRO 자동 승급제</div>",
      "<div style='font-size:11.5px;color:#cbd5e1;line-height:1.5;'>" + uxEsc(nextMsg) + "</div>",
      "<div style='width:100%;height:8px;border-radius:999px;background:#020617;border:1px solid #1e293b;overflow:hidden;margin:6px 0 4px;'>",
      "  <div style='height:100%;width:" + pct + "%;background:linear-gradient(90deg,#10b981,#38bdf8);border-radius:999px;transition:width .3s ease;'></div>",
      "</div>",
      "<div style='display:flex;justify-content:space-between;font-size:10.5px;color:#94a3b8;margin-bottom:6px;'>",
      "  <span>출석 +20P · 분석글 +15P · 추천받기 +2P</span>",
      "  <span>" + (nk ? ("연동: " + uxEsc(nk)) : "PIN 미연동") + "</span>",
      "</div>",
      "<button type='button' id='sqTierOpenHubBtn' style='width:100%;border:0;border-radius:11px;padding:9px 12px;font-size:12px;font-weight:900;cursor:pointer;background:linear-gradient(135deg,#34d399,#38bdf8);color:#020617;'>🔐 4자리 PIN 계정연동 · 🛍️ 포인트 상점</button>"
    ].join("");

    var openBtn = document.getElementById("sqTierOpenHubBtn");
    if (openBtn) {
      openBtn.onclick = function () {
        var hubBtn = document.getElementById("sqOpenMemberHubBtn");
        if (hubBtn) { hubBtn.click(); return; }
        var modal = document.getElementById("sqMemberHubModal");
        if (modal) modal.style.display = "flex";
      };
    }
  }

  function installUnifiedAttendanceV22() {
    window.checkAttendance = function () {
      var nk = getSavedNickV22();
      var pn = getSavedPinV22();
      var cPts = (typeof state === "object" && state) ? Number(state.points || 120) : 120;
      var cStrk = (typeof state === "object" && state) ? Number(state.streak || 0) : 0;

      fetch("/api/member/checkin", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          nickname: nk,
          pin: pn,
          actor_key: getActorKeyV22(),
          client_points: cPts,
          client_streak: cStrk
        })
      })
        .then(function (r) { return r.json().then(function (d) { return { status: r.status, data: d }; }); })
        .then(function (res) {
          var d = res.data || {};
          if (res.status !== 200 || !d.ok) {
            if (typeof showToast === "function") showToast("❌ " + (d.detail || "출석 처리 중 오류가 발생했습니다."));
            return;
          }
          if (typeof state === "object" && state) {
            state.checkedToday = true;
            state.points = Number(d.points != null ? d.points : cPts);
            state.streak = Number(d.streak_days != null ? d.streak_days : (cStrk + 1));
            try { localStorage.setItem("sportiq_checked", d.last_checkin_date || getTodayKstStr()); } catch (e) {}
            if (typeof window.updatePointsUI === "function") window.updatePointsUI();
            if (d.role_tier && typeof window.setRole === "function") {
              window.setRole(d.role_tier, false);
            }
          }
          renderTierUpgradeSidebarCard();
          if (typeof showToast === "function") showToast(d.message || "출석 체크 완료! +20P 적립");
        })
        .catch(function () {
          if (typeof showToast === "function") showToast("일시적인 통신 오류입니다. 잠시 후 다시 시도해 주세요.");
        });
    };
  }

  // -------------------------------------------------------------------------
  // 3. Server DB Persistence for Community Post Likes & Bookmarks + Bookmark Filter Pill
  // -------------------------------------------------------------------------
  function applyReactionStateToPosts() {
    if (typeof state !== "object" || !state || !Array.isArray(state.posts)) return;
    state.posts.forEach(function (p) {
      if (!p || !p.id) return;
      if (reactionCache.like_counts[p.id] !== undefined) {
        p.likes = Number(reactionCache.like_counts[p.id]);
      }
      p.liked = Boolean(reactionCache.liked_ids[p.id]);
      p.bookmarked = Boolean(reactionCache.bookmarked_ids[p.id]);
    });
    updateBookmarkFilterPillCount();
  }

  function updateBookmarkFilterPillCount() {
    var pill = document.getElementById("sqFeedBookmarkFilterBtn");
    if (!pill) return;
    var cnt = Object.keys(reactionCache.bookmarked_ids || {}).filter(function (k) {
      return Boolean(reactionCache.bookmarked_ids[k]);
    }).length;
    pill.textContent = "⭐ 내 북마크 (" + cnt + ")";
    pill.style.background = bookmarkOnlyFilter ? "rgba(245, 158, 11, 0.24)" : "rgba(15, 23, 42, 0.9)";
    pill.style.borderColor = bookmarkOnlyFilter ? "#fbbf24" : "#334155";
    pill.style.color = bookmarkOnlyFilter ? "#fde68a" : "#cbd5e1";
  }

  function mountBookmarkFilterPill() {
    if (document.getElementById("sqFeedBookmarkFilterBtn")) return;
    var feedList = document.getElementById("feedList");
    if (!feedList || !feedList.parentElement) return;
    var filterRow = null;
    var candidates = feedList.parentElement.querySelectorAll("button");
    for (var i = 0; i < candidates.length; i++) {
      var t = candidates[i].textContent || "";
      if (t.indexOf("전체 피드") !== -1 || t.indexOf("회원 분석글") !== -1) {
        filterRow = candidates[i].parentElement;
        break;
      }
    }
    if (!filterRow) return;
    var btn = document.createElement("button");
    btn.type = "button";
    btn.id = "sqFeedBookmarkFilterBtn";
    btn.style.cssText = "border:1px solid #334155;border-radius:999px;padding:5px 12px;font-size:12px;font-weight:800;cursor:pointer;transition:all .15s;";
    btn.onclick = function () {
      bookmarkOnlyFilter = !bookmarkOnlyFilter;
      updateBookmarkFilterPillCount();
      if (typeof window.renderFeed === "function") window.renderFeed();
    };
    filterRow.appendChild(btn);
    updateBookmarkFilterPillCount();
  }

  function hydratePostsAndReactionsFromServer() {
    var ak = getActorKeyV22();
    var nk = getSavedNickV22();
    var url = "/api/posts?actor_key=" + encodeURIComponent(ak) + "&nickname=" + encodeURIComponent(nk);
    fetch(url)
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.ok || typeof state !== "object" || !state) return;
        reactionCache.like_counts = d.like_counts || {};
        reactionCache.liked_ids = {};
        (d.liked_ids || []).forEach(function (id) { reactionCache.liked_ids[id] = true; });
        reactionCache.bookmarked_ids = {};
        (d.bookmarked_ids || []).forEach(function (id) { reactionCache.bookmarked_ids[id] = true; });

        var existingMap = {};
        (state.posts || []).forEach(function (p) { if (p && p.id) existingMap[p.id] = p; });
        var incoming = (d.posts || []).filter(function (p) { return p && p.id && !existingMap[p.id]; });
        if (incoming.length) {
          state.posts = incoming.concat(state.posts || []);
        } else if ((!state.posts || state.posts.length === 0) && Array.isArray(d.posts)) {
          state.posts = d.posts.slice();
        }
        applyReactionStateToPosts();
        mountBookmarkFilterPill();
        if (typeof window.renderFeed === "function") window.renderFeed();
      })
      .catch(function () {});
  }

  function installPersistentLikesAndBookmarks() {
    window.toggleLike = function (id) {
      if (typeof state !== "object" || !state || !Array.isArray(state.posts)) return;
      var p = state.posts.find(function (x) { return x && x.id === id; });
      if (!p) return;
      var baseLikes = Number(p.likes || 1);
      var nextLiked = !p.liked;
      p.liked = nextLiked;
      p.likes = Math.max(0, baseLikes + (nextLiked ? 1 : -1));
      reactionCache.liked_ids[id] = nextLiked;
      reactionCache.like_counts[id] = p.likes;
      if (typeof window.renderFeed === "function") window.renderFeed();

      fetch("/api/posts/" + encodeURIComponent(id) + "/like", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          actor_key: getActorKeyV22(),
          nickname: getSavedNickV22(),
          base_likes: baseLikes
        })
      })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (!d || !d.ok) return;
          p.liked = Boolean(d.liked);
          p.likes = Number(d.likes);
          reactionCache.liked_ids[id] = p.liked;
          reactionCache.like_counts[id] = p.likes;
          if (typeof window.renderFeed === "function") window.renderFeed();
          if (typeof showToast === "function") {
            if (d.liked) {
              showToast(d.author_rewarded_points ? "👍 추천이 영구 반영되었습니다! (작성자에게 +2P 적립)" : "👍 추천이 서버에 영구 저장되었습니다.");
            } else {
              showToast("추천을 취소했습니다.");
            }
          }
        })
        .catch(function () {});
    };

    window.toggleBookmark = function (id) {
      if (typeof state !== "object" || !state || !Array.isArray(state.posts)) return;
      var p = state.posts.find(function (x) { return x && x.id === id; });
      if (!p) return;
      p.bookmarked = !p.bookmarked;
      reactionCache.bookmarked_ids[id] = p.bookmarked;
      updateBookmarkFilterPillCount();
      if (typeof window.renderFeed === "function") window.renderFeed();

      fetch("/api/posts/" + encodeURIComponent(id) + "/bookmark", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          actor_key: getActorKeyV22(),
          nickname: getSavedNickV22(),
          title: p.title || "",
          league: p.league || "",
          author: p.author || ""
        })
      })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (!d || !d.ok) return;
          p.bookmarked = Boolean(d.bookmarked);
          reactionCache.bookmarked_ids[id] = p.bookmarked;
          updateBookmarkFilterPillCount();
          if (typeof window.renderFeed === "function") window.renderFeed();
          if (typeof showToast === "function") {
            showToast(p.bookmarked ? "⭐ 북마크가 서버에 영구 저장되었습니다." : "북마크를 해제했습니다.");
          }
        })
        .catch(function () {});
    };

    if (typeof window.renderFeed === "function" && !window.__sqFeedBookmarkWrapV22) {
      window.__sqFeedBookmarkWrapV22 = true;
      var prevRenderFeed = window.renderFeed;
      window.renderFeed = function () {
        applyReactionStateToPosts();
        prevRenderFeed();
        mountBookmarkFilterPill();
        var box = document.getElementById("feedList");
        if (!box) return;
        if (bookmarkOnlyFilter) {
          var visibleCnt = 0;
          box.querySelectorAll("article").forEach(function (art) {
            var bmBtn = art.querySelector("button[onclick^='toggleBookmark(']");
            var m = bmBtn ? (bmBtn.getAttribute("onclick") || "").match(/toggleBookmark\(['"]([^'"]+)['"]\)/) : null;
            var pid = m ? m[1] : "";
            var keep = Boolean(pid && reactionCache.bookmarked_ids[pid]);
            art.style.display = keep ? "" : "none";
            if (keep) visibleCnt++;
          });
          var emptyNote = document.getElementById("sqBookmarkEmptyHint");
          if (visibleCnt === 0) {
            if (!emptyNote) {
              emptyNote = document.createElement("div");
              emptyNote.id = "sqBookmarkEmptyHint";
              emptyNote.style.cssText = "padding:24px;text-align:center;color:#94a3b8;font-size:13px;border:1px dashed #334155;border-radius:14px;";
              emptyNote.textContent = "아직 저장한 북마크 게시글이 없습니다. 관심 있는 분석글의 '★ 북마크' 버튼을 눌러보세요!";
              box.appendChild(emptyNote);
            }
          } else if (emptyNote && emptyNote.parentNode) {
            emptyNote.parentNode.removeChild(emptyNote);
          }
        } else {
          var oldNote = document.getElementById("sqBookmarkEmptyHint");
          if (oldNote && oldNote.parentNode) oldNote.parentNode.removeChild(oldNote);
        }
      };
    }
  }

  // Sync PIN-linked members into Analyst Ranking -> Live Member Point Ranking
  function mountLivePinMembersBannerInRanking() {
    var rankSec = document.getElementById("tab-ranking");
    if (!rankSec || document.getElementById("sqLivePinMemberBoard")) return;
    fetch("/api/member/live-ranking")
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.ok || !Array.isArray(d.members) || d.members.length === 0) return;
        if (document.getElementById("sqLivePinMemberBoard")) return;
        var card = document.createElement("div");
        card.id = "sqLivePinMemberBoard";
        card.style.cssText = "background:rgba(15,23,42,.88);border:1px solid rgba(52,211,153,.35);border-radius:16px;padding:14px 16px;margin-bottom:14px;";
        var rowsHtml = d.members.slice(0, 8).map(function (m) {
          var medal = m.rank === 1 ? "🥇" : (m.rank === 2 ? "🥈" : (m.rank === 3 ? "🥉" : "#" + m.rank));
          var tierTag = m.role_tier === "PRO" ? "👑 PRO" : (m.role_tier === "MEMBER" ? "✅ MEMBER" : "🌱 GUEST");
          return (
            "<div style='display:flex;justify-content:space-between;align-items:center;padding:6px 10px;border-radius:10px;background:rgba(2,6,23,.55);border:1px solid rgba(51,65,85,.5);font-size:12px;'>" +
            "  <span><strong style='color:#fde68a;margin-right:6px;'>" + medal + "</strong><strong style='color:#f8fafc;'>" + uxEsc(m.nickname) + "</strong> " +
            (m.active_badge ? "<span style='color:#38bdf8;font-size:11px;margin-left:4px;'>" + uxEsc(m.active_badge) + "</span>" : "") +
            "  <span style='color:#94a3b8;font-size:11px;margin-left:6px;'>(" + tierTag + " · 연속 " + m.streak_days + "일)</span></span>" +
            "  <strong style='color:#34d399;font-weight:900;'>" + m.points + " P</strong>" +
            "</div>"
          );
        }).join("");
        card.innerHTML = [
          "<div style='display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px;margin-bottom:10px;'>",
          "  <span style='font-size:13.5px;font-weight:900;color:#6ee7b7;'>🔐 실시간 4자리 PIN 인증 회원 포인트 TOP 랭킹</span>",
          "  <span style='font-size:11px;color:#94a3b8;'>출석(+20P)·분석글(+15P)·추천(+2P) 실시간 DB 연동</span>",
          "</div>",
          "<div style='display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:8px;'>" + rowsHtml + "</div>"
        ].join("");
        rankSec.insertBefore(card, rankSec.firstChild);
      })
      .catch(function () {});
  }

  // -------------------------------------------------------------------------
  // 4. Overseas Sports News Korean Summary Badges + NFL / UFC / F1 Filters
  // -------------------------------------------------------------------------
  var OVERSEAS_FILTER_TABS_V22 = [
    ["전체", "🌏 전체"],
    ["축구", "⚽ 축구"],
    ["야구", "⚾ 야구"],
    ["농구", "🏀 농구"],
    ["하키", "🏒 하키"],
    ["NFL", "🏈 NFL"],
    ["UFC", "🥊 UFC"],
    ["F1", "🏎️ F1"]
  ];

  function renderOverseasFilterBarV22() {
    var bar = document.getElementById("rssSportFilters");
    if (!bar) return;
    bar.innerHTML = OVERSEAS_FILTER_TABS_V22.map(function (pair) {
      var sp = pair[0];
      var label = pair[1];
      var cnt = overseasCountsV22[sp];
      var active = overseasActiveSportV22 === sp;
      var cls = active
        ? "rss-filter-btn rounded-lg bg-cyan-500/25 border border-cyan-400 px-2.5 py-1 text-xs font-extrabold text-cyan-200 transition"
        : "rss-filter-btn rounded-lg bg-slate-900/90 border border-slate-700 px-2.5 py-1 text-xs font-semibold text-slate-300 hover:border-slate-500 hover:text-white transition";
      return (
        "<button type='button' data-rss-sport='" + uxEsc(sp) + "' class='" + cls + "'>" +
        uxEsc(label) + (cnt != null ? " <span style='opacity:.82;'>(" + cnt + ")</span>" : "") +
        "</button>"
      );
    }).join("");

    bar.querySelectorAll("[data-rss-sport]").forEach(function (btn) {
      btn.onclick = function () {
        var sp = btn.getAttribute("data-rss-sport") || "전체";
        window.filterRssNews(sp);
      };
    });
  }

  function renderOverseasListV22() {
    var box = document.getElementById("rssNewsList");
    if (!box) return;
    var sp = overseasActiveSportV22 || "전체";
    var list = sp === "전체"
      ? overseasCacheV22
      : overseasCacheV22.filter(function (it) { return (it.sport_ko || it.sport) === sp; });

    if (!list.length) {
      box.innerHTML = "<div style='padding:18px;text-align:center;color:#94a3b8;font-size:12.5px;'>선택한 종목의 해외 실시간 속보를 불러오는 중입니다...</div>";
      return;
    }

    box.innerHTML = list.slice(0, 24).map(function (it) {
      var sBadge = it.sport_badge || ("⚽ " + (it.sport_ko || "해외스포츠"));
      var sColor = it.sport_color || "#38bdf8";
      var tBadge = it.topic_badge || "⚡ 현지 주요속보";
      var tColor = it.topic_color || "#7dd3fc";
      var koSum = it.ko_summary || "해외 주요 매체 실시간 스포츠 속보";
      return [
        "<a href='" + uxEsc(it.url || it.link || "#") + "' target='_blank' rel='noopener noreferrer' class='sq-rss-card-v22'>",
        "  <div style='display:flex;justify-content:space-between;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:6px;'>",
        "    <div style='display:inline-flex;align-items:center;gap:5px;flex-wrap:wrap;'>",
        "      <span style='background:rgba(15,23,42,.95);border:1px solid " + uxEsc(sColor) + ";color:" + uxEsc(sColor) + ";border-radius:999px;padding:1.5px 8px;font-size:11px;font-weight:800;'>" + uxEsc(sBadge) + "</span>",
        "      <span style='background:rgba(30,41,59,.85);border:1px solid rgba(148,163,184,.3);color:" + uxEsc(tColor) + ";border-radius:999px;padding:1.5px 8px;font-size:11px;font-weight:800;'>" + uxEsc(tBadge) + "</span>",
        "      <span style='font-size:11px;color:#94a3b8;font-weight:700;'>" + uxEsc(it.source || "BBC·ESPN") + "</span>",
        "    </div>",
        "    <span style='font-size:11px;color:#64748b;'>" + uxEsc(it.published_kst || it.pubDate || "") + " ↗</span>",
        "  </div>",
        "  <div style='background:rgba(6,182,212,.1);border-left:3px solid #22d3ee;border-radius:6px;padding:4px 9px;margin-bottom:6px;font-size:11.8px;font-weight:800;color:#cffafe;'>🇰🇷 한글 요약: " + uxEsc(koSum) + "</div>",
        "  <div style='font-size:13.2px;font-weight:700;color:#f8fafc;line-height:1.45;'>" + uxEsc(it.title || "") + "</div>",
        "</a>"
      ].join("");
    }).join("");
  }

  function installOverseasNewsUpgradeV22() {
    window.filterRssNews = function (sp) {
      overseasActiveSportV22 = sp || "전체";
      try { window.rssActiveSport = overseasActiveSportV22; } catch (e) {}
      renderOverseasFilterBarV22();
      renderOverseasListV22();
    };
    window.renderRssNews = function () {
      renderOverseasFilterBarV22();
      renderOverseasListV22();
    };
    window.loadRssNews = function () {
      renderOverseasFilterBarV22();
      fetch("/api/lounge/overseas-news")
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (!d || !d.ok) return;
          overseasCacheV22 = Array.isArray(d.items) ? d.items : [];
          overseasCountsV22 = d.counts || {};
          try { window.rssNewsCache = overseasCacheV22; } catch (e) {}
          renderOverseasFilterBarV22();
          renderOverseasListV22();
        })
        .catch(function () {});
    };

    if (document.getElementById("rssSportFilters") || document.getElementById("rssNewsList")) {
      window.loadRssNews();
    }
  }

  // -------------------------------------------------------------------------
  // 5. Official Bottom Non-Betting Legal & Trust Footer + Policy Modal
  // -------------------------------------------------------------------------
  var LEGAL_DOCS_V22 = {
    terms: {
      title: "📜 SportIQ 서비스 이용약관 (요약본)",
      body: [
        "제1조 (서비스 목적) SportIQ(billnojay.com)는 국내외 공식 스포츠 경기 일정, 전력 통계, 기대득점(xG)·세이버메트릭스 분석 리포트 및 건전한 스포츠 팬 의견 교류 공간을 제공하는 순수 스포츠 데이터 정보 플랫폼입니다.",
        "제2조 (사행성·불법 베팅 절대 금지) 본 플랫폼은 현금·환전·배당 베팅 중개나 사행성 게임을 일절 운영하거나 중개하지 않으며, 화면 내 승부예측 투표와 포인트(P)는 금전적 가치가 없는 무료 커뮤니티 활동 지표(칭호 장착 및 주간 이벤트 응모용)로만 사용됩니다.",
        "제3조 (이용자 의무) 타인 비방, 욕설, 허위사실 유포 및 불법 사설 도박 사이트 유도·홍보 게시물 작성 시 사전 경고 없이 즉시 삭제 및 영구 이용 제한 조치됩니다."
      ]
    },
    privacy: {
      title: "🔐 개인정보 비수집 · 4자리 PIN 보안 안내",
      body: [
        "1. 개인식별정보 무수집 원칙: SportIQ 오픈 라운지는 주민등록번호, 휴대전화번호, 이메일, 실명 등 민감 개인정보를 회원가입 목적으로 수집하지 않습니다.",
        "2. 4자리 PIN 단방향 암호화 저장: 기기 간 포인트·칭호·북마크 연동을 위해 설정하는 숫자 4자리 PIN은 평문으로 저장되지 않으며, 서버 솔트 기반 PBKDF2-HMAC-SHA256(12,000회 반복) 단방향 해시값으로만 안전하게 보관됩니다.",
        "3. 제휴 문의 보호: 파트너 입점·제휴 문의에 기재된 연락처는 공개 목록에서 즉시 마스킹 처리되며 제휴 회신 목적 외에는 사용되지 않습니다."
      ]
    },
    policy: {
      title: "🛡️ 건전 커뮤니티 운영정책 · 청소년 보호안내",
      body: [
        "1. 불법 사행성 홍보 무관용 원칙: 불법 스포츠도박 링크, 단톡방·텔레그램 유도, 코드 공유 게시물은 자동 필터 및 신고 즉시 블라인드 처리되며 국민체육진흥공단 불법스포츠토토 신고센터(1899-1119) 기준에 따라 엄정 대응합니다.",
        "2. 데이터 출처 및 통계 면책: 경기 스코어, 순위, 부상·이적 리포트 및 AI 승리 확률 시뮬레이션은 ESPN·BBC 등 공식 공개 피드와 통계 모델(Elo·Monte Carlo)에 기반한 참고 지표이며 경기 결과를 보장하지 않습니다.",
        "3. 건전 토론 문화: 전술·데이터 기반의 상호 존중 분석 문화를 지향하며, 게시글 신고 누적 시 피드에서 자동 숨김 처리됩니다."
      ]
    }
  };

  function openLegalModalV22(docKey) {
    var info = LEGAL_DOCS_V22[docKey] || LEGAL_DOCS_V22.terms;
    var modal = document.getElementById("sqLegalPolicyModal");
    if (!modal) {
      modal = document.createElement("div");
      modal.id = "sqLegalPolicyModal";
      modal.style.cssText = "position:fixed;inset:0;z-index:9998;background:rgba(2,6,23,.82);backdrop-filter:blur(4px);display:none;align-items:center;justify-content:center;padding:16px;";
      modal.innerHTML = [
        "<div style='max-width:580px;width:100%;background:#0f172a;border:1px solid #334155;border-radius:18px;padding:20px;box-shadow:0 24px 50px rgba(0,0,0,.6);color:#e2e8f0;'>",
        "  <div style='display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:12px;border-bottom:1px solid #1e293b;padding-bottom:10px;'>",
        "    <h3 id='sqLegalModalTitle' style='margin:0;font-size:16px;font-weight:900;color:#f8fafc;'></h3>",
        "    <button type='button' id='sqCloseLegalModalBtn' style='background:#1e293b;border:1px solid #475569;color:#cbd5e1;border-radius:8px;padding:4px 10px;font-size:12px;font-weight:800;cursor:pointer;'>✕ 닫기 (Esc)</button>",
        "  </div>",
        "  <div style='display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px;'>",
        "    <button type='button' data-legal-tab='terms' class='sq-footer-link-btn'>서비스 이용약관</button>",
        "    <button type='button' data-legal-tab='privacy' class='sq-footer-link-btn'>개인정보 · 4자리 PIN 보안</button>",
        "    <button type='button' data-legal-tab='policy' class='sq-footer-link-btn'>건전 커뮤니티 운영정책</button>",
        "  </div>",
        "  <div id='sqLegalModalBody' style='font-size:13px;color:#cbd5e1;line-height:1.72;display:flex;flex-direction:column;gap:10px;'></div>",
        "</div>"
      ].join("");
      document.body.appendChild(modal);
      document.getElementById("sqCloseLegalModalBtn").onclick = function () { modal.style.display = "none"; };
      modal.addEventListener("click", function (ev) {
        if (ev.target === modal) modal.style.display = "none";
      });
      modal.querySelectorAll("[data-legal-tab]").forEach(function (b) {
        b.onclick = function () { openLegalModalV22(b.getAttribute("data-legal-tab")); };
      });
      window.addEventListener("keydown", function (ev) {
        if ((ev.key === "Escape" || ev.keyCode === 27) && modal.style.display !== "none") {
          modal.style.display = "none";
        }
      });
    }
    document.getElementById("sqLegalModalTitle").textContent = info.title;
    document.getElementById("sqLegalModalBody").innerHTML = info.body.map(function (line) {
      return "<div style='background:rgba(2,6,23,.6);border:1px solid #1e293b;border-radius:10px;padding:10px 12px;'>" + uxEsc(line) + "</div>";
    }).join("");
    modal.style.display = "flex";
  }

  function mountOfficialLegalFooterV22() {
    if (document.getElementById("sqOfficialLegalFooter")) return;
    var footer = document.createElement("footer");
    footer.id = "sqOfficialLegalFooter";
    footer.setAttribute("role", "contentinfo");
    footer.innerHTML = [
      "<div style='max-width:1240px;margin:0 auto;'>",
      "  <div style='background:rgba(16,185,129,.08);border:1px solid rgba(52,211,153,.32);border-radius:14px;padding:12px 15px;margin-bottom:16px;display:flex;align-items:flex-start;gap:10px;'>",
      "    <span style='font-size:18px;line-height:1.2;'>⚖️</span>",
      "    <div>",
      "      <div style='font-weight:900;color:#6ee7b7;font-size:12.5px;margin-bottom:2px;'>건전 스포츠 데이터 분석 플랫폼 공식 고지 (비베팅 · 사행성 무관)</div>",
      "      <div style='color:#cbd5e1;font-size:11.8px;'>SportIQ(billnojay.com)는 국내외 공식 경기 일정, 순위, 기대득점(xG)·세이버메트릭스 통계 및 팬 전술 분석을 다루는 <strong>순수 스포츠 데이터 정보 커뮤니티</strong>입니다. 본 서비스는 어떠한 형태의 현금 베팅·환전·불법 스포츠도박 중개 또는 사행성 사이트 홍보도 일절 취급하지 않으며, 라운지 포인트(P)는 금전적 가치가 없는 커뮤니티 활동 지표입니다.</div>",
      "    </div>",
      "  </div>",
      "  <div style='display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;'>",
      "    <div>",
      "      <div style='font-weight:900;color:#f8fafc;font-size:13.5px;'>SportIQ Sports Data Intelligence Lounge · billnojay.com</div>",
      "      <div style='color:#64748b;font-size:11.5px;margin-top:2px;'>실시간 글로벌 스포츠 통계 · AI 시뮬레이션 · 4자리 PIN 비식별 보안 라운지 | 불법스포츠도박 신고센터 1899-1119</div>",
      "    </div>",
      "    <div style='display:flex;gap:7px;flex-wrap:wrap;'>",
      "      <button type='button' class='sq-footer-link-btn' data-sq-legal='terms'>서비스 이용약관</button>",
      "      <button type='button' class='sq-footer-link-btn' data-sq-legal='privacy'>개인정보 · 4자리 PIN 보안안내</button>",
      "      <button type='button' class='sq-footer-link-btn' data-sq-legal='policy'>건전 커뮤니티 운영정책</button>",
      "      <button type='button' class='sq-footer-link-btn' id='sqFooterPartnerJumpBtn'>파트너·제휴 문의</button>",
      "    </div>",
      "  </div>",
      "</div>"
    ].join("");
    document.body.appendChild(footer);

    footer.querySelectorAll("[data-sq-legal]").forEach(function (btn) {
      btn.onclick = function () { openLegalModalV22(btn.getAttribute("data-sq-legal")); };
    });
    var pJump = document.getElementById("sqFooterPartnerJumpBtn");
    if (pJump) {
      pJump.onclick = function () {
        var btns = document.querySelectorAll("button");
        for (var i = 0; i < btns.length; i++) {
          if ((btns[i].textContent || "").indexOf("입점·제휴 문의") !== -1) {
            btns[i].scrollIntoView({ behavior: "smooth", block: "center" });
            btns[i].click();
            return;
          }
        }
        window.scrollTo({ top: 0, behavior: "smooth" });
      };
    }
  }

  function bootMemberUxV22() {
    injectUxV22Styles();
    enforceSingleLineMainTabBar();
    syncTop20MatchCountBadge();
    installUnifiedAttendanceV22();
    renderTierUpgradeSidebarCard();
    installPersistentLikesAndBookmarks();
    hydratePostsAndReactionsFromServer();
    mountLivePinMembersBannerInRanking();
    installOverseasNewsUpgradeV22();
    mountOfficialLegalFooterV22();

    // Keep dynamic UI states synchronized when tabs switch or async data arrives
    setInterval(function () {
      enforceSingleLineMainTabBar();
      syncTop20MatchCountBadge();
      renderTierUpgradeSidebarCard();
    }, 2500);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bootMemberUxV22);
  } else {
    bootMemberUxV22();
  }
})();
'''


def _ensure_upgrade_js_dedup_patched() -> bool:
    global _SORT_DEDUP_APPLIED, _AI_SEARCH_UI_PATCHED
    js_raw = getattr(_smu, "UPGRADE_JS", "")
    if not isinstance(js_raw, str) or not js_raw:
        return False
    updated = js_raw
    if "cleanupDuplicateMatchSortBars" not in updated:
        p1 = re.compile(
            r"function\s+sortRenderedMatchCardStrips\s*\(\s*forceResort\s*\)\s*\{.*?(?=\n\s*function\s+mountSortToggleAboveStrip\s*\()",
            flags=re.DOTALL,
        )
        updated, n1 = p1.subn(lambda _m: _FIXED_SORT_STRIP_FN, updated, count=1)
        if n1 > 0:
            _SORT_DEDUP_APPLIED = True
    else:
        _SORT_DEDUP_APPLIED = True

    if "sqAiCatBar" not in updated:
        p2 = re.compile(
            r"function\s+renderAiIqSearchResult\s*\(\s*d\s*\)\s*\{.*?(?=\n\s*function\s+openAiIqModal\s*\()",
            flags=re.DOTALL,
        )
        updated, n2 = p2.subn(lambda _m: _ENHANCED_RENDER_AI_IQ_FN, updated, count=1)
        if n2 > 0:
            _AI_SEARCH_UI_PATCHED = True
    else:
        _AI_SEARCH_UI_PATCHED = True

    if "__SPORTIQ_MEMBER_UX_V22__" not in updated:
        updated = updated + "\n" + _FRONTEND_V22_JS

    if updated != js_raw:
        _smu.UPGRADE_JS = updated
    return _SORT_DEDUP_APPLIED or _AI_SEARCH_UI_PATCHED or ("__SPORTIQ_MEMBER_UX_V22__" in updated)


_ensure_upgrade_js_dedup_patched()


install_sqlite_guard()

router = APIRouter()

KST = timezone(timedelta(hours=9))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(BASE_DIR, "sportiq_community.html")
DB_PATH = os.environ.get("SPORTIQ_LOUNGE_DB", "/tmp/sportiq_lounge.db")


def _to_epoch_ts(val: Any) -> Optional[float]:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    if not s:
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        pass
    try:
        iso = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=KST)
        return dt.timestamp()
    except Exception:
        return None


def _format_cheer_time(val: Any) -> str:
    ts = _to_epoch_ts(val)
    if ts is not None:
        try:
            return datetime.fromtimestamp(ts, tz=KST).strftime("%H:%M:%S")
        except (OSError, OverflowError, ValueError):
            pass
    s = str(val or "").strip()
    if len(s) >= 8 and ":" in s:
        for token in s.split():
            if token.count(":") >= 1 and token[0].isdigit():
                return token[:8]
    return datetime.now(KST).strftime("%H:%M:%S")


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=DEFAULT_TIMEOUT_SEC, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={DEFAULT_BUSY_TIMEOUT_MS};")
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def _db_session(write: bool = False):
    return managed_sqlite_conn(DB_PATH, write=write, row_factory=sqlite3.Row)


def _init_db() -> None:
    def _op() -> None:
        with _db_session(write=False) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS presence_sessions (
                    session_id TEXT PRIMARY KEY,
                    role TEXT DEFAULT 'GUEST',
                    visit_date TEXT NOT NULL,
                    first_seen REAL NOT NULL,
                    last_seen REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS daily_hashtags (
                    tag TEXT PRIMARY KEY,
                    count INTEGER NOT NULL DEFAULT 1,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS partner_inquiries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    company TEXT NOT NULL,
                    contact_name TEXT NOT NULL,
                    contact_info TEXT NOT NULL,
                    tier TEXT DEFAULT 'STANDARD',
                    message TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cheers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    author TEXT NOT NULL,
                    role TEXT DEFAULT 'GUEST',
                    team TEXT DEFAULT 'ALL',
                    message TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS post_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    post_id TEXT NOT NULL,
                    session_id TEXT DEFAULT '',
                    reason TEXT DEFAULT '부적절한 게시글',
                    created_at REAL NOT NULL
                );
CREATE TABLE IF NOT EXISTS global_match_rankings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_key TEXT UNIQUE NOT NULL,
    sport TEXT NOT NULL,
    league TEXT NOT NULL,
    league_color TEXT NOT NULL,
    matchup TEXT NOT NULL,
    schedule_venue TEXT NOT NULL,
    votes INTEGER NOT NULL DEFAULT 0,
    buzz_score INTEGER NOT NULL DEFAULT 80,
    rank_change TEXT NOT NULL DEFAULT '-'
);
INSERT OR IGNORE INTO global_match_rankings(match_key, sport, league, league_color, matchup, schedule_venue, votes, buzz_score, rank_change) VALUES
('epl-liv-mun', 'football', 'EPL', '#38003c', '리버풀 vs 맨체스터 유나이티드', '11.22 · 안필드 노스웨스트 더비', 2840, 99, '-'), ('unl-esp-eng', 'football', '네이션스리그 A', '#003399', '스페인 vs 잉글랜드', '11.16 · 매치데이 6 빅매치', 2610, 97, '▲1'),
('ucl-ars-bvb', 'football', '챔피언스리그', '#0e1e5b', '아스널 vs 보루시아 도르트문트', '11.25 · 매치데이 5', 2390, 95, '▲2'), ('nba-bos-dal', 'basketball', 'NBA', '#c9082a', '보스턴 셀틱스 vs 댈러스 매버릭스', '11.16 · TD 가든', 2150, 93, '▼1'), ('epl-new-ars', 'football', 'EPL', '#38003c', '뉴캐슬 유나이티드 vs 아스널', '11.21 · 세인트 제임스 파크', 1980, 91, '▲1'),
('nba-phx-lal', 'basketball', 'NBA', '#c9082a', '피닉스 선즈 vs LA 레이커스', '11.16 · 서부 라이벌전', 1840, 89, '-'), ('laliga-bar-vil', 'football', '라리가', '#ee8707', 'FC 바르셀로나 vs 비야레알', '11.22 · 스포티파이 캄 노우', 1690, 87, 'NEW'), ('nfl-det-ne', 'nfl', 'NFL', '#013369', '디트로이트 라이온스 vs 뉴잉글랜드 패트리어츠', '11.15 · 뮌헨 인터내셔널 게임', 1540, 85, '▲2'),
('laliga-rma-cel', 'football', '라리가', '#ee8707', '레알 마드리드 vs 셀타 비고', '11.22 · 산티아고 베르나베우', 1420, 83, '▼1'), ('nba-den-phx', 'basketball', 'NBA', '#c9082a', '덴버 너기츠 vs 피닉스 선즈', '11.17 · 볼 아레나', 1290, 81, 'NEW');
UPDATE global_match_rankings SET votes = 2980 WHERE match_key = 'epl-liv-mun' AND votes < 2980; INSERT OR IGNORE INTO global_match_rankings(match_key, sport, league, league_color, matchup, schedule_venue, votes, buzz_score, rank_change) VALUES ('apbc-kor-jpn', 'baseball', 'APBC 야구 결승', '#0047a0', '대한민국 vs 일본', '11.15 · 도쿄돔 결승 한일전', 2890, 98, 'HOT'), ('nhl-fla-edm', 'hockey', 'NHL 하키', '#0284c7', '플로리다 팬서스 vs 에드먼턴 오일러스', '11.17 · 스탠리컵 리매치', 2040, 92, 'NEW'), ('nhl-chi-tor', 'hockey', 'NHL 하키', '#0284c7', '시카고 블랙호크스 vs 토론토 메이플리프스', '11.17 · 오리지널 6 매치', 1760, 88, 'NEW'), ('apbc-tpe-aus', 'baseball', 'APBC 야구', '#0047a0', '대만 vs 호주', '11.15 · 도쿄돔 3·4위 결정전', 1610, 86, 'NEW'), ('seriea-ata-int', 'football', '세리에A', '#008fd7', '아탈란타 vs 인터 밀란', '11.22 · 베르가모 상위권 격돌', 1480, 84, 'NEW'), ('nhl-nyr-mtl', 'hockey', 'NHL 하키', '#0284c7', '뉴욕 레인저스 vs 몬트리올 캐나디언스', '11.16 · 매디슨 스퀘어 가든', 1360, 82, 'NEW'), ('lidom-lic-esc', 'baseball', 'LIDOM 야구', '#0047a0', '티그레스 델 리세이 vs 에스코히도', '11.15 · 도미니카 윈터리그 더비', 1210, 79, 'NEW'), ('ucl-lil-bay', 'football', '챔피언스리그', '#0e1e5b', '릴 OSC vs 바이에른 뮌헨', '11.25 · 매치데이 5', 1150, 78, '▲1'), ('nhl-tbl-edm', 'hockey', 'NHL 하키', '#0284c7', '탬파베이 라이트닝 vs 에드먼턴 오일러스', '11.19 · 아말리 아레나', 1090, 77, 'NEW'), ('abl-ade-bri', 'baseball', 'ABL 프로야구', '#0047a0', '애들레이드 자이언츠 vs 브리즈번 밴디츠', '11.20 · 호주리그 개막전', 1020, 76, 'NEW');            """
            )
            row = conn.execute("SELECT COUNT(*) AS cnt FROM daily_hashtags").fetchone()
            if row["cnt"] == 0:
                now = time.time()
                seeds = [
                    ("#UCL결승", 142),
                    ("#손흥민", 128),
                    ("#EPL전술", 96),
                    ("#KBO리그", 84),
                    ("#NBA플레이오프", 79),
                    ("#xG데이터", 63),
                    ("#T1월즈", 58),
                ]
                conn.executemany(
                    "INSERT OR IGNORE INTO daily_hashtags(tag, count, updated_at) VALUES (?, ?, ?)",
                    [(t, c, now) for t, c in seeds],
                )
            crow = conn.execute("SELECT COUNT(*) AS cnt FROM cheers").fetchone()
            if crow["cnt"] == 0:
                now = time.time()
                cseeds = [
                    ("seed-1", "데이터헌터", "PRO", "토트넘", "오늘 후반전 하프스페이스 침투 xG 수치가 심상치 않네요!", now - 180),
                    ("seed-2", "야구덕후K", "MEMBER", "KIA", "선발 투수 릴리스 포인트 안정적이라 6이닝 무실점 기대합니다.", now - 90),
                    ("seed-3", "클러치타임", "GUEST", "NBA", "4쿼터 클러치 수비 로테이션이 승부를 가를 듯합니다!", now - 30),
                ]
                conn.executemany(
                    "INSERT INTO cheers(session_id, author, role, team, message, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    cseeds,
                )

    run_with_db_retry(_op)


_init_db()

# ============================================================================
# AI 아이큐 Hyper Unified Search Engine v2.0 (High Recall + High Precision)
# ============================================================================
_HYPER_SEARCH_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_HYPER_SEARCH_LOCK = threading.Lock()

# 135+ Bilingual Korean <-> English Sports Club, Star Athlete, League & Intent Synonym Groups
_SPORTIQ_SYNONYM_GROUPS: Tuple[Tuple[str, ...], ...] = (
    # Korean Overseas Stars
    ("손흥민", "쏘니", "son heung-min", "heung-min son", "sonny", "토트넘", "tottenham"),
    ("김민재", "kim min-jae", "min-jae kim", "바이에른 뮌헨", "bayern munich"),
    ("이강인", "lee kang-in", "kang-in lee", "파리 생제르맹", "psg", "paris saint-germain"),
    ("황희찬", "황소", "hwang hee-chan", "hee-chan hwang", "울버햄튼", "wolves", "wolverhampton"),
    ("양민혁", "yang min-hyeok", "qpr", "토트넘"),
    ("배준호", "bae jun-ho", "스토크", "stoke city"),
    ("설영우", "seol young-woo", "즈베즈다", "crvena zvezda"),
    ("이재성", "lee jae-sung", "마인츠", "mainz"),
    ("홍현석", "hong hyun-seok", "마인츠", "mainz"),
    ("조규성", "cho gue-sung", "미트윌란", "midtjylland"),
    ("오타니", "오타니 쇼헤이", "shohei ohtani", "ohtani", "다저스", "dodgers"),
    ("이정후", "jung hoo lee", "샌프란시스코 자이언츠", "san francisco giants", "sf giants"),
    ("김하성", "ha-seong kim", "탬파베이", "파드리스", "rays", "padres"),
    ("류현진", "ryu hyun-jin", "한화 이글스"),
    ("김도영", "kim do-young", "기아 타이거즈", "kia 타이거즈"),
    ("페이커", "이상혁", "faker", "t1", "티원"),
    # EPL & European Football Clubs
    ("맨유", "맨체스터 유나이티드", "맨체스터유나이티드", "manchester united", "man utd", "man united", "mufc", "루벤 아모림", "amorim"),
    ("맨시티", "맨체스터 시티", "맨체스터시티", "manchester city", "man city", "mcfc"),
    ("아스널", "아스날", "arsenal", "gunners"),
    ("리버풀", "liverpool", "lfc", "안필드"),
    ("토트넘", "토트넘 홋스퍼", "스퍼스", "tottenham", "tottenham hotspur", "spurs"),
    ("첼시", "chelsea", "cfc"),
    ("뉴캐슬", "뉴캐슬 유나이티드", "newcastle", "newcastle united"),
    ("애스턴 빌라", "아스톤 빌라", "아스톤빌라", "aston villa"),
    ("브라이튼", "브라이턴", "brighton"),
    ("웨스트햄", "west ham"),
    ("울버햄튼", "울버햄프턴", "wolves", "wolverhampton"),
    ("풀럼", "fulham"),
    ("본머스", "bournemouth"),
    ("브렌트포드", "brentford"),
    ("노팅엄", "노팅엄 포레스트", "nottingham forest"),
    ("에버턴", "에버튼", "everton"),
    ("레알", "레알 마드리드", "레알마드리드", "real madrid"),
    ("바르샤", "바르셀로나", "fc 바르셀로나", "barcelona", "fc barcelona", "barca"),
    ("아틀레티코", "아틀레티코 마드리드", "꼬마", "atletico madrid", "atletico"),
    ("비야레알", "villarreal"),
    ("소시에다드", "레알 소시에다드", "real sociedad"),
    ("셀타", "셀타 비고", "celta vigo", "celta"),
    ("바이에른", "바이에른 뮌헨", "뮌헨", "bayern", "bayern munich"),
    ("도르트문트", "보루시아 도르트문트", "dortmund", "borussia dortmund", "bvb"),
    ("레버쿠젠", "바이엘 레버쿠젠", "leverkusen", "bayer leverkusen"),
    ("라이프치히", "rb 라이프치히", "leipzig", "rb leipzig"),
    ("인테르", "인터 밀란", "인터밀란", "inter", "inter milan", "internazionale"),
    ("ac밀란", "ac 밀란", "밀란", "ac milan"),
    ("유벤투스", "유베", "juventus", "juve"),
    ("나폴리", "napoli"),
    ("아탈란타", "atalanta"),
    ("로마", "as 로마", "as roma"),
    ("파리 생제르맹", "파리생제르맹", "psg", "paris saint-germain"),
    ("릴", "릴 osc", "lille"),
    ("모나코", "as 모나코", "as monaco"),
    # National Teams
    ("대한민국", "한국 축구", "한국 대표팀", "홍명보호", "south korea", "korea republic"),
    ("일본", "일본 대표팀", "japan"),
    ("스페인", "spain"),
    ("잉글랜드", "england"),
    ("프랑스", "france"),
    ("독일", "germany"),
    ("브라질", "brazil"),
    ("아르헨티나", "메시", "lionel messi", "argentina"),
    ("포르투갈", "호날두", "cristiano ronaldo", "portugal"),
    # NBA Teams & Stars
    ("레이커스", "la 레이커스", "lakers", "los angeles lakers", "르브론", "lebron james", "lebron"),
    ("워리어스", "골든스테이트", "골스", "warriors", "golden state warriors", "커리", "스테픈 커리", "stephen curry", "curry"),
    ("보스턴", "셀틱스", "보스턴 셀틱스", "celtics", "boston celtics", "테이텀", "jayson tatum"),
    ("댈러스", "매버릭스", "댈러스 매버릭스", "mavericks", "dallas mavericks", "mavs", "돈치치", "luka doncic"),
    ("덴버", "너기츠", "덴버 너기츠", "nuggets", "denver nuggets", "요키치", "니콜라 요키치", "nikola jokic", "jokic"),
    ("피닉스", "선즈", "피닉스 선즈", "suns", "phoenix suns", "듀란트", "kevin durant"),
    ("밀워키", "벅스", "밀워키 벅스", "bucks", "milwaukee bucks", "야니스", "giannis"),
    ("닉스", "뉴욕 닉스", "knicks", "new york knicks"),
    ("썬더", "오클라호마시티", "thunder", "okc thunder", "길저스알렉산더"),
    ("클리퍼스", "la 클리퍼스", "clippers", "la clippers"),
    ("스퍼스 nba", "샌안토니오", "웸반야마", "victor wembanyama", "wembanyama", "san antonio spurs"),
    # MLB & KBO Baseball
    ("다저스", "la 다저스", "dodgers", "los angeles dodgers"),
    ("양키스", "뉴욕 양키스", "yankees", "new york yankees", "애런 저지", "aaron judge"),
    ("메츠", "뉴욕 메츠", "mets", "new york mets", "후안 소토", "juan soto"),
    ("파드리스", "샌디에이고", "padres", "san diego padres"),
    ("필리스", "필라델피아", "phillies", "philadelphia phillies"),
    ("기아", "kia", "기아 타이거즈", "kia 타이거즈", "기아타이거즈", "kia tigers"),
    ("삼성", "삼성 라이온즈", "삼성라이온즈", "samsung lions"),
    ("엘지", "lg", "엘지 트윈스", "lg 트윈스", "lg트윈스", "lg twins"),
    ("두산", "두산 베어스", "두산베어스", "doosan bears"),
    ("케이티", "kt", "케이티 위즈", "kt 위즈", "kt wiz"),
    ("ssg", "랜더스", "ssg 랜더스", "ssg landers"),
    ("롯데", "롯데 자이언츠", "롯데자이언츠", "lotte giants"),
    ("한화", "한화 이글스", "한화이글스", "hanwha eagles"),
    ("엔씨", "nc", "엔씨 다이노스", "nc 다이노스", "nc dinos"),
    ("키움", "키움 히어로즈", "키움히어로즈", "kiwoom heroes"),
    # NHL, NFL, Esports, UFC, Tennis, F1, V-League
    ("오일러스", "에드먼턴 오일러스", "edmonton oilers", "oilers", "맥데이비드", "connor mcdavid"),
    ("팬서스", "플로리다 팬서스", "florida panthers"),
    ("메이플리프스", "토론토 메이플리프스", "toronto maple leafs", "maple leafs"),
    ("레인저스 nhl", "뉴욕 레인저스", "new york rangers"),
    ("블랙호크스", "시카고 블랙호크스", "chicago blackhawks"),
    ("라이트닝", "탬파베이 라이트닝", "tampa bay lightning"),
    ("치프스", "캔자스시티 치프스", "kansas city chiefs", "chiefs", "마홈스", "patrick mahomes"),
    ("라이온스 nfl", "디트로이트 라이온스", "detroit lions"),
    ("패트리어츠", "뉴잉글랜드 패트리어츠", "new england patriots"),
    ("젠지", "gen.g", "geng", "쵸비", "chovy"),
    ("한화생명e스포츠", "한화생명", "hle"),
    ("디플러스 기아", "디플러스", "dk", "dplus kia"),
    ("페르스타펜", "막스 페르스타펜", "max verstappen", "verstappen", "레드불 레이싱", "red bull racing"),
    ("노리스", "랜도 노리스", "lando norris", "맥라렌", "mclaren"),
    ("페라리", "샤를 르클레르", "루이스 해밀턴", "ferrari", "leclerc", "lewis hamilton"),
    ("알카라스", "카를로스 알카라스", "carlos alcaraz", "alcaraz"),
    ("시너", "야닉 시너", "jannik sinner", "sinner"),
    ("조코비치", "노박 조코비치", "novak djokovic", "djokovic"),
    ("페레이라", "알렉스 페레이라", "alex pereira", "마카체프", "islam makhachev", "존 존스", "jon jones"),
    ("흥국생명", "김연경", "현대건설", "대한항공 점보스", "현대캐피탈", "v리그", "v-리그"),
    # Leagues & Sports
    ("epl", "프리미어리그", "잉글랜드 프리미어리그", "premier league"),
    ("라리가", "스페인 라리가", "laliga", "la liga"),
    ("분데스리가", "분데스", "bundesliga"),
    ("세리에a", "세리에", "serie a"),
    ("리그1", "리그앙", "ligue 1"),
    ("챔스", "챔피언스리그", "유럽챔피언스리그", "ucl", "champions league"),
    ("네이션스리그", "유럽네이션스리그", "unl", "nations league"),
    ("k리그", "k리그1", "kleague", "k league"),
    ("kbo", "프로야구", "한국야구"),
    ("mlb", "메이저리그", "미국야구"),
    ("nba", "미국프로농구", "프로농구"),
    ("nhl", "북미아이스하키", "아이스하키"),
    ("nfl", "미식축구", "슈퍼볼"),
    ("lck", "롤챔스", "리그오브레전드", "월즈", "롤드컵"),
    ("ufc", "종합격투기", "옥타곤", "mma"),
    ("f1", "포뮬러1", "포뮬러원", "formula 1", "그랑프리"),
    ("atp", "테니스", "윔블던", "전미오픈", "호주오픈"),
    # Tactical & Search Intent Concepts
    ("부상", "부상자", "결장", "결장자", "명단제외", "출전불투명", "복귀", "라인업", "선발", "injury", "injured", "injuries", "lineup", "out", "doubtful"),
    ("이적", "오피셜", "영입", "방출", "재계약", "임대", "몸값", "transfer", "signing", "official", "contract", "rumor", "loan"),
    ("배당", "드롭오즈", "오즈", "승률", "승부예측", "픽", "언오버", "핸디캡", "odds", "prediction", "preview", "betting"),
    ("순위", "승점", "테이블", "성적", "랭킹", "standings", "table", "ranking"),
    ("득점", "골", "하이라이트", "멀티골", "결승골", "결과", "스코어", "goal", "score", "highlights", "result"),
    ("전술", "압박", "빌드업", "기대득점", "xg", "포메이션", "tactics", "formation"),
)

# Ambiguous corporate/city short names that require explicit sports context on open web news
_AMBIGUOUS_NON_SPORT_TERMS: Set[str] = {
    "기아", "kia", "삼성", "엘지", "lg", "한화", "롯데", "두산", "케이티", "kt", "현캐",
    "현대", "흥국", "대한항공", "파리", "보스턴", "밀란", "로마", "나폴리", "댈러스", "피닉스", "덴버", "시카고",
}

_NON_SPORT_SPAM_KEYWORDS: Tuple[str, ...] = (
    "코스피", "코스닥", "목표주가", "주가", "매수", "매도", "영업이익", "배당금", "주식", "증권",
    "아파트", "분양", "청약", "재건축", "부동산", "대통령", "국회", "의원", "정당", "날씨", "미세먼지",
    "신차", "전기차", "쏘렌토", "카니발", "스포티지", "갤럭시", "냉장고", "백화점",
)

_SPORT_POSITIVE_ANCHORS: Tuple[str, ...] = (
    "축구", "야구", "농구", "배구", "하키", "테니스", "격투기", "경기", "선수", "감독", "코치", "구단",
    "리그", "시즌", "득점", "실점", "승리", "패배", "무승부", "연승", "연패", "결승", "준결승", "선발",
    "부상", "결장", "이적", "영입", "골", "어시스트", "홈런", "타율", "투수", "타자", "이닝", "리바운드",
    "플레이오프", "챔피언스", "프리미어", "라리가", "분데스", "세리에", "올림픽", "월드컵", "대표팀",
    "epl", "ucl", "unl", "nba", "mlb", "kbo", "nhl", "nfl", "ufc", "f1", "lck", "atp", "fc", "vs",
    "score", "goal", "match", "game", "coach", "player", "injury", "transfer", "league", "win", "cup",
)

_TRUSTED_SPORT_SOURCES: Tuple[str, ...] = (
    "espn", "bbc", "sky sports", "the athletic", "goal", "fourfourtwo", "포포투", "인터풋볼",
    "스포탈코리아", "스포티비", "spotv", "풋볼리스트", "베스트일레븐", "일간스포츠", "스포츠조선",
    "스포츠서울", "스포츠동아", "스포츠경향", "osen", "오센", "마이데일리", "엠스플", "엑스포츠",
    "한스경제", "mk스포츠", "점프볼", "루키", "인벤", "디스이즈게임", "데일리e스포츠",
)


# Canonical Wikipedia Page Titles (KO, EN) for Sports Entities (prevents corporate/car/city/medical false hits!)
_CANONICAL_WIKI_TITLES: Tuple[Tuple[Tuple[str, ...], str, str], ...] = (
    (("맨유", "맨체스터 유나이티드", "맨체스터유나이티드", "manchester united", "man utd", "man united"), "맨체스터 유나이티드 FC", "Manchester United F.C."),
    (("맨시티", "맨체스터 시티", "맨체스터시티", "manchester city", "man city"), "맨체스터 시티 FC", "Manchester City F.C."),
    (("아스널", "아스날", "arsenal"), "아스널 FC", "Arsenal F.C."),
    (("리버풀", "liverpool"), "리버풀 FC", "Liverpool F.C."),
    (("토트넘", "스퍼스", "tottenham", "spurs"), "토트넘 홋스퍼 FC", "Tottenham Hotspur F.C."),
    (("첼시", "chelsea"), "첼시 FC", "Chelsea F.C."),
    (("뉴캐슬", "newcastle"), "뉴캐슬 유나이티드 FC", "Newcastle United F.C."),
    (("애스턴 빌라", "아스톤 빌라", "아스톤빌라", "aston villa"), "애스턴 빌라 FC", "Aston Villa F.C."),
    (("손흥민", "쏘니", "son heung-min", "heung-min son"), "손흥민", "Son Heung-min"),
    (("김민재", "kim min-jae"), "김민재 (축구 선수)", "Kim Min-jae"),
    (("이강인", "lee kang-in"), "이강인", "Lee Kang-in"),
    (("황희찬", "hwang hee-chan"), "황희찬", "Hwang Hee-chan"),
    (("레알", "레알 마드리드", "real madrid"), "레알 마드리드 CF", "Real Madrid CF"),
    (("바르샤", "바르셀로나", "barcelona"), "FC 바르셀로나", "FC Barcelona"),
    (("아틀레티코", "atletico madrid"), "아틀레티코 마드리드", "Atlético Madrid"),
    (("바이에른", "바이에른 뮌헨", "뮌헨", "bayern"), "FC 바이에른 뮌헨", "FC Bayern Munich"),
    (("도르트문트", "dortmund"), "보루시아 도르트문트", "Borussia Dortmund"),
    (("레버쿠젠", "leverkusen"), "바이어 04 레버쿠젠", "Bayer 04 Leverkusen"),
    (("인테르", "인터 밀란", "인터밀란", "inter milan"), "FC 인테르나치오날레 밀라노", "Inter Milan"),
    (("ac밀란", "ac 밀란", "밀란", "ac milan"), "AC 밀란", "AC Milan"),
    (("유벤투스", "유베", "juventus"), "유벤투스 FC", "Juventus FC"),
    (("나폴리", "napoli"), "SSC 나폴리", "SSC Napoli"),
    (("파리 생제르맹", "psg", "paris saint-germain"), "파리 생제르맹 FC", "Paris Saint-Germain F.C."),
    (("기아", "kia", "기아 타이거즈", "kia 타이거즈", "kia tigers"), "KIA 타이거즈", "Kia Tigers"),
    (("삼성", "삼성 라이온즈", "samsung lions"), "삼성 라이온즈", "Samsung Lions"),
    (("엘지", "lg", "엘지 트윈스", "lg 트윈스", "lg twins"), "LG 트윈스", "LG Twins"),
    (("두산", "두산 베어스", "doosan bears"), "두산 베어스", "Doosan Bears"),
    (("케이티", "kt", "케이티 위즈", "kt 위즈", "kt wiz"), "KT 위즈", "KT Wiz"),
    (("ssg", "랜더스", "ssg 랜더스", "ssg landers"), "SSG 랜더스", "SSG Landers"),
    (("롯데", "롯데 자이언츠", "lotte giants"), "롯데 자이언츠", "Lotte Giants"),
    (("한화", "한화 이글스", "hanwha eagles"), "한화 이글스", "Hanwha Eagles"),
    (("엔씨", "nc", "엔씨 다이노스", "nc 다이노스", "nc dinos"), "NC 다이노스", "NC Dinos"),
    (("키움", "키움 히어로즈", "kiwoom heroes"), "키움 히어로즈", "Kiwoom Heroes"),
    (("오타니", "shohei ohtani", "ohtani"), "오타니 쇼헤이", "Shohei Ohtani"),
    (("다저스", "la 다저스", "dodgers"), "로스앤젤레스 다저스", "Los Angeles Dodgers"),
    (("양키스", "yankees"), "뉴욕 양키스", "New York Yankees"),
    (("이정후", "jung hoo lee"), "이정후", "Jung Hoo Lee"),
    (("김도영", "kim do-young"), "김도영 (야구 선수)", "Kim Do-young"),
    (("류현진", "ryu hyun-jin"), "류현진", "Ryu Hyun-jin"),
    (("보스턴", "셀틱스", "보스턴 셀틱스", "celtics"), "보스턴 셀틱스", "Boston Celtics"),
    (("레이커스", "lakers"), "로스앤젤레스 레이커스", "Los Angeles Lakers"),
    (("워리어스", "골든스테이트", "골스", "warriors"), "골든스테이트 워리어스", "Golden State Warriors"),
    (("매버릭스", "댈러스", "mavericks"), "댈러스 매버릭스", "Dallas Mavericks"),
    (("너기츠", "덴버", "nuggets"), "덴버 너기츠", "Denver Nuggets"),
    (("요키치", "jokic"), "니콜라 요키치", "Nikola Jokic"),
    (("커리", "stephen curry"), "스테픈 커리", "Stephen Curry"),
    (("르브론", "lebron"), "르브론 제임스", "LeBron James"),
    (("페이커", "faker", "t1", "티원"), "페이커", "Faker (gamer)"),
    (("젠지", "gen.g"), "젠지", "Gen.G"),
    (("페르스타펜", "verstappen"), "막스 페르스타펜", "Max Verstappen"),
    (("노리스", "lando norris"), "랜도 노리스", "Lando Norris"),
)

_INTENT_ONLY_TOKENS: Set[str] = {
    "부상", "부상자", "결장", "결장자", "명단제외", "출전불투명", "복귀", "라인업", "선발",
    "injury", "injured", "injuries", "lineup", "out", "doubtful",
    "이적", "오피셜", "영입", "방출", "재계약", "임대", "몸값", "transfer", "signing", "official", "contract", "rumor", "loan",
    "배당", "드롭오즈", "오즈", "승률", "승부예측", "픽", "언오버", "핸디캡", "odds", "prediction", "preview", "betting",
    "순위", "승점", "테이블", "성적", "랭킹", "standings", "table", "ranking",
    "득점", "골", "하이라이트", "멀티골", "결승골", "결과", "스코어", "goal", "score", "highlights", "result",
    "전술", "압박", "빌드업", "기대득점", "xg", "포메이션", "tactics", "formation", "일정", "중계", "뉴스", "속보",
}

_WIKI_NON_SPORT_BLOCKLIST: Tuple[str, ...] = (
    "자동차 제조", "현대자동차그룹", "쏘렌토", "카니발", "스포티지", "모닝", "세단", "전기차", "suv",
    "기아 (기업)", "삼성전자", "lg전자", "두산중공업", "롯데그룹", "한화그룹",
    "연평해전", "해전", "전투", "전사자", "포피", "피부", "생식기", "수술", "질병",
    "잉글랜드의 도시", "그레이터맨체스터주의 도시", "city and metropolitan borough",
    "스페인의 수도", "이탈리아의 도시", "독일의 도시", "프랑스의 수도",
)

_WIKI_SPORT_REQUIRED_ANCHORS: Tuple[str, ...] = (
    "축구", "야구", "농구", "배구", "하키", "테니스", "격투기", "골프", "포뮬러", "e스포츠", "프로게이머",
    "구단", "클럽", "선수", "감독", "리그", "프리미어리그", "라리가", "분데스리가", "세리에", "챔피언스리그",
    "kbo", "프로야구", "메이저리그", "월드컵", "올림픽", "포지션", "공격수", "수비수", "미드필더", "골키퍼",
    "투수", "타자", "내야수", "외야수", "홈구장", "스타디움",
    "football", "soccer", "baseball", "basketball", "hockey", "esports", "formula one", "racing",
    "premier league", "la liga", "bundesliga", "serie a", "mlb", "nba", "nhl", "nfl", "ufc",
    "professional footballer", "baseball player", "basketball player", "sports club", "football club",
)


def _resolve_wiki_queries(q_clean: str, stems: List[str], en_terms: List[str]) -> Tuple[str, str]:
    q_low = q_clean.lower()
    for aliases, ko_title, en_title in _CANONICAL_WIKI_TITLES:
        if any(a == st or (len(a) >= 2 and a in q_low) for a in aliases for st in (stems or [q_low])):
            return ko_title, en_title
    entity_stems = [s for s in stems if s not in _INTENT_ONLY_TOKENS]
    base_ko = " ".join(entity_stems) if entity_stems else q_clean
    en_entities = [e for e in en_terms if e.lower() not in _INTENT_ONLY_TOKENS]
    base_en = en_entities[0] if en_entities else ""
    return base_ko, base_en


def _is_valid_sports_wiki_card(card: Dict[str, Any]) -> bool:
    title_l = str(card.get("title") or "").lower()
    sum_l = str(card.get("summary") or "").lower()
    comb = f"{title_l} {sum_l}"
    if any(blk in comb for blk in _WIKI_NON_SPORT_BLOCKLIST):
        # Allow only if title itself explicitly says FC / 타이거즈 / 라이온즈 / 트윈스 / 베어스 / 축구 선수 / 야구 선수
        if not any(ok_t in title_l for ok_t in ("fc", "타이거즈", "라이온즈", "트윈스", "베어스", "위즈", "랜더스", "자이언츠", "이글스", "다이노스", "히어로즈", "셀틱스", "레이커스", "선수")):
            return False
    return any(anch in comb for anch in _WIKI_SPORT_REQUIRED_ANCHORS)

_JOSA_SUFFIXES: Tuple[str, ...] = (
    "에서는", "으로는", "에게는", "로부터", "과의", "와의", "에서", "에게", "으로", "까지", "부터",
    "이랑", "하고", "보다", "처럼", "만큼", "은", "는", "이", "가", "을", "를", "의", "에",
    "과", "와", "도", "만", "로", "전",
)


def _strip_korean_josa(token: str) -> str:
    t = token.strip()
    if len(t) <= 2:
        return t
    for josa in _JOSA_SUFFIXES:
        if t.endswith(josa) and len(t) - len(josa) >= 2:
            stem = t[: -len(josa)]
            if re.search(r"[가-힣0-9a-zA-Z]", stem):
                return stem
    return t


def _analyze_query_concepts(raw_query: str) -> Dict[str, Any]:
    q_clean = re.sub(r"[^\w\s가-힣\.\-]", " ", (raw_query or "").strip())
    q_clean = re.sub(r"\s+", " ", q_clean).strip()
    q_low = q_clean.lower()

    raw_parts = [p for p in q_low.split() if p and p not in ("vs", "v", "대", "및", "관련", "최근", "오늘")]
    stems: List[str] = []
    for p in raw_parts:
        st = _strip_korean_josa(p)
        if st and st not in stems:
            stems.append(st)
    if not stems and q_low:
        stems = [q_low]

    concept_groups: List[Set[str]] = []
    expanded_display: List[str] = []
    en_terms: List[str] = []
    ko_terms: List[str] = []
    matched_group_indices: Set[int] = set()

    # 1. Match multi-word and single-word synonym groups
    for idx, grp in enumerate(_SPORTIQ_SYNONYM_GROUPS):
        grp_low = tuple(x.lower() for x in grp)
        hit = False
        for alias in grp_low:
            if len(alias) >= 2 and (alias in q_low or any(alias == st or (len(st) >= 2 and st == alias) for st in stems)):
                hit = True
                break
        if hit:
            matched_group_indices.add(idx)
            cset = set(grp_low)
            concept_groups.append(cset)
            for a in grp:
                if re.search(r"[a-zA-Z]", a) and a.lower() not in [e.lower() for e in en_terms]:
                    en_terms.append(a)
                elif re.search(r"[가-힣]", a) and a not in ko_terms:
                    ko_terms.append(a)
                if a not in expanded_display:
                    expanded_display.append(a)

    # 2. Add any remaining stems not covered by a synonym group as their own concept group
    covered_tokens = {tok for cg in concept_groups for tok in cg}
    for st in stems:
        if len(st) < 2:
            continue
        if any(st in tok or tok in st for tok in covered_tokens):
            continue
        concept_groups.append({st})
        if st not in expanded_display:
            expanded_display.insert(0, st)
        if re.search(r"[a-zA-Z]", st):
            en_terms.append(st)
        else:
            ko_terms.append(st)

    if not concept_groups and q_low:
        concept_groups.append({q_low})

    # Determine if query needs explicit sport disambiguator on general news engines
    needs_sport_guard = any(st in _AMBIGUOUS_NON_SPORT_TERMS for st in stems) or (
        len(stems) == 1 and not any(anch in q_low for anch in _SPORT_POSITIVE_ANCHORS) and not matched_group_indices
    )

    ko_primary_q = f"{q_clean} 스포츠" if needs_sport_guard else q_clean
    ko_alt_q = ""
    if ko_terms:
        alt_candidates = [k for k in ko_terms if k.lower() not in q_low][:2]
        if alt_candidates:
            ko_alt_q = f"{stems[0]} {alt_candidates[0]}"

    en_search_q = " ".join(en_terms[:3]) if en_terms else (q_clean if re.search(r"[a-zA-Z]", q_clean) else "")

    # Detect relevant DataCenter league code for targeted standings/injuries lookup
    league_hint = "EPL"
    lg_detect_map = (
        (("라리가", "laliga", "la liga", "레알", "바르샤", "바르셀로나", "아틀레티코", "비야레알", "셀타"), "LALIGA"),
        (("분데스", "bundesliga", "바이에른", "뮌헨", "김민재", "도르트문트", "레버쿠젠", "이재성", "마인츠"), "BUNDESLIGA"),
        (("세리에", "serie a", "인테르", "인터밀란", "유벤투스", "밀란", "나폴리", "아탈란타", "로마"), "SERIEA"),
        (("리그1", "리그앙", "ligue 1", "파리", "psg", "이강인", "릴", "모나코"), "LIGUE1"),
        (("k리그", "kleague", "울산", "전북", "포항", "서울", "강원", "수원"), "KLEAGUE"),
        (("kbo", "프로야구", "기아", "kia", "김도영", "삼성 라이온즈", "lg 트윈스", "두산", "롯데", "한화", "류현진", "kt 위즈", "ssg", "nc", "키움"), "KBO"),
        (("mlb", "메이저리그", "오타니", "다저스", "이정후", "김하성", "양키스", "메츠", "파드리스"), "MLB"),
        (("nba", "농구", "레이커스", "르브론", "워리어스", "커리", "셀틱스", "보스턴", "매버릭스", "돈치치", "너기츠", "요키치", "선즈", "벅스"), "NBA"),
        (("nhl", "하키", "오일러스", "맥데이비드", "팬서스", "메이플리프스", "블랙호크스"), "NHL"),
        (("lck", "페이커", "t1", "티원", "젠지", "한화생명", "디플러스"), "LCK"),
    )
    joined_all = (q_low + " " + " ".join(expanded_display)).lower()
    for kws, lg_code in lg_detect_map:
        if any(k in joined_all for k in kws):
            league_hint = lg_code
            break

    entity_groups = [
        cg for cg in concept_groups
        if not all(tok in _INTENT_ONLY_TOKENS for tok in cg)
    ] or concept_groups
    wiki_ko_q, wiki_en_q = _resolve_wiki_queries(q_clean, stems, en_terms)

    return {
        "raw_query": q_clean,
        "q_low": q_low,
        "stems": stems,
        "concept_groups": concept_groups,
        "entity_groups": entity_groups,
        "wiki_ko_q": wiki_ko_q,
        "wiki_en_q": wiki_en_q,
        "expanded_terms": expanded_display[:8],
        "ko_primary_q": ko_primary_q,
        "ko_alt_q": ko_alt_q,
        "en_search_q": en_search_q,
        "needs_sport_guard": needs_sport_guard,
        "league_hint": league_hint,
    }


def _score_text_relevance(
    title: str,
    body: str,
    source: str,
    published_ts: float,
    q_meta: Dict[str, Any],
    is_internal_sportiq: bool = False,
) -> int:
    """Computes a 0..99 precision score combining concept coverage, exact phrase, domain purity & recency."""
    t_low = (title or "").lower()
    b_low = (body or "").lower()
    full_low = f"{t_low} {b_low}"
    src_low = (source or "").lower()

    # 1. Anti-noise filter for external web news (eliminates stock, real-estate, appliance, politics false positives!)
    if not is_internal_sportiq:
        has_spam = any(sp in full_low for sp in _NON_SPORT_SPAM_KEYWORDS)
        has_sport = any(sa in full_low for sa in _SPORT_POSITIVE_ANCHORS)
        if has_spam and not has_sport:
            return 0
        if q_meta.get("needs_sport_guard") and not has_sport:
            return 0

    groups: List[Set[str]] = (
        q_meta.get("entity_groups") or q_meta["concept_groups"]
        if is_internal_sportiq
        else q_meta["concept_groups"]
    )
    if not groups:
        return 60

    matched_groups = 0
    title_group_hits = 0
    for cg in groups:
        in_t = any(alias in t_low for alias in cg if len(alias) >= 2)
        in_b = in_t or any(alias in b_low for alias in cg if len(alias) >= 2)
        if in_t:
            title_group_hits += 1
            matched_groups += 1
        elif in_b:
            matched_groups += 1

    if matched_groups == 0:
        return 0

    coverage_ratio = matched_groups / len(groups)
    # Require all concepts if 2 concepts, or >= 65% if 3+ concepts, unless exact query string is in title
    q_low = q_meta["q_low"]
    exact_in_title = len(q_low) >= 2 and (q_low in t_low)

    score = 38.0 * coverage_ratio + 22.0 * (title_group_hits / len(groups))
    if coverage_ratio >= 0.999:
        score += 18.0  # Full AND-match bonus!
    if exact_in_title:
        score += 12.0

    # Trusted sports media & internal intent alignment bonus
    if is_internal_sportiq:
        score += 12.0
        for full_cg in q_meta["concept_groups"]:
            if any(a in full_low for a in full_cg if len(a) >= 2):
                score += 4.0
    elif any(ts in src_low for ts in _TRUSTED_SPORT_SOURCES):
        score += 6.0

    # Recency bonus (up to +8 pts)
    if published_ts > 1000000:
        age_hours = max(0.0, (time.time() - published_ts) / 3600.0)
        if age_hours <= 6:
            score += 8.0
        elif age_hours <= 24:
            score += 6.0
        elif age_hours <= 72:
            score += 3.5
        elif age_hours <= 168:
            score += 1.5

    # Reject weak single-term matches when user asked a specific multi-concept query (e.g. "맨유 부상")
    if len(groups) >= 2 and matched_groups == 1 and not exact_in_title:
        # Must at least match the primary (first) entity concept in title!
        first_cg = groups[0]
        if not any(a in t_low for a in first_cg if len(a) >= 2):
            return 0
        score = min(score, 74.0)

    return max(55, min(99, int(round(score))))


def _http_fetch_bytes(url: str, timeout: float = 3.2) -> Optional[bytes]:
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
                "Accept": "application/rss+xml, application/xml, application/json, text/xml, */*",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if 200 <= resp.status < 300:
                return resp.read()
    except Exception:
        return None
    return None


def _parse_rss_pubdate(pub_raw: str) -> Tuple[float, str]:
    s = (pub_raw or "").strip()
    if s:
        try:
            dt = parsedate_to_datetime(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            ts = dt.timestamp()
            return ts, datetime.fromtimestamp(ts, tz=KST).strftime("%m.%d %H:%M")
        except Exception:
            pass
    now = time.time()
    return now - 3600, datetime.now(KST).strftime("%m.%d")


def _clean_rss_text(raw: str) -> str:
    txt = _html_lib.unescape(raw or "")
    txt = re.sub(r"<[^>]+>", " ", txt)
    return re.sub(r"\s+", " ", txt).strip()


def _fetch_gnews_rss(query: str, lang: str = "ko", limit: int = 16) -> List[Dict[str, Any]]:
    if not query.strip():
        return []
    if lang == "ko":
        params = urllib.parse.urlencode({"q": query, "hl": "ko", "gl": "KR", "ceid": "KR:ko"})
    else:
        params = urllib.parse.urlencode({"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"})
    raw = _http_fetch_bytes(f"https://news.google.com/rss/search?{params}", timeout=3.2)
    if not raw:
        return []
    out: List[Dict[str, Any]] = []
    try:
        root = ET.fromstring(raw)
        for it in root.findall(".//item")[:limit]:
            raw_title = _clean_rss_text(it.findtext("title") or "")
            link = (it.findtext("link") or "").strip()
            src_el = it.find("source")
            source = _clean_rss_text(src_el.text if src_el is not None and src_el.text else "")
            if " - " in raw_title and not source:
                raw_title, source = raw_title.rsplit(" - ", 1)
            elif source and raw_title.endswith(" - " + source):
                raw_title = raw_title[: -(len(source) + 3)].strip()
            desc = _clean_rss_text(it.findtext("description") or "")
            if desc and raw_title and desc.startswith(raw_title[:25]):
                desc = desc[len(raw_title):].strip(" -·")
            ts, kst_lbl = _parse_rss_pubdate(it.findtext("pubDate") or "")
            if raw_title and link:
                out.append(
                    {
                        "title": raw_title,
                        "url": link,
                        "source": source or ("Google 뉴스" if lang == "ko" else "Global Sports"),
                        "published_kst": kst_lbl,
                        "published_ts": ts,
                        "snippet": desc[:130],
                        "lang_badge": "국내" if lang == "ko" else "해외원문",
                    }
                )
    except Exception:
        pass
    return out


def _fetch_bing_rss(query: str, mkt: str = "ko-KR", limit: int = 12) -> List[Dict[str, Any]]:
    if not query.strip():
        return []
    params = urllib.parse.urlencode({"q": query, "format": "rss", "mkt": mkt})
    raw = _http_fetch_bytes(f"https://www.bing.com/news/search?{params}", timeout=3.2)
    if not raw:
        return []
    out: List[Dict[str, Any]] = []
    try:
        root = ET.fromstring(raw)
        for it in root.findall(".//item")[:limit]:
            title = _clean_rss_text(it.findtext("title") or "")
            link = (it.findtext("link") or "").strip()
            desc = _clean_rss_text(it.findtext("description") or "")
            ts, kst_lbl = _parse_rss_pubdate(it.findtext("pubDate") or "")
            src = "Bing 스포츠" if mkt == "ko-KR" else "Global Wire"
            for child in list(it):
                if child.tag.lower().endswith("source") and child.text:
                    src = _clean_rss_text(child.text)
                    break
            if title and link:
                out.append(
                    {
                        "title": title,
                        "url": link,
                        "source": src,
                        "published_kst": kst_lbl,
                        "published_ts": ts,
                        "snippet": desc[:130],
                        "lang_badge": "국내" if mkt == "ko-KR" else "해외원문",
                    }
                )
    except Exception:
        pass
    return out


def _fetch_espn_public_search(en_query: str, limit: int = 8) -> List[Dict[str, Any]]:
    if not en_query.strip():
        return []
    params = urllib.parse.urlencode({"query": en_query, "limit": str(limit)})
    raw = _http_fetch_bytes(f"https://site.web.api.espn.com/apis/search/v2?{params}", timeout=3.0)
    if not raw:
        return []
    out: List[Dict[str, Any]] = []
    try:
        data = json.loads(raw.decode("utf-8"))
        for group in data.get("results") or []:
            gtype = str(group.get("type") or "")
            for c in (group.get("contents") or [])[:limit]:
                title = _clean_rss_text(str(c.get("displayName") or c.get("title") or ""))
                desc = _clean_rss_text(str(c.get("description") or c.get("subtitle") or ""))
                link_obj = c.get("link") or {}
                web_url = str(link_obj.get("web") or "") if isinstance(link_obj, dict) else ""
                if not web_url:
                    for lk in c.get("links") or []:
                        if isinstance(lk, dict) and lk.get("href"):
                            web_url = str(lk["href"])
                            break
                if title and web_url:
                    out.append(
                        {
                            "title": title,
                            "url": web_url,
                            "source": "ESPN Official" if gtype != "player" else "ESPN 선수 프로필",
                            "published_kst": "ESPN 실시간",
                            "published_ts": time.time() - 1800,
                            "snippet": desc[:130],
                            "lang_badge": "ESPN",
                        }
                    )
    except Exception:
        pass
    return out


def _fetch_wikipedia_smart(query: str, lang: str = "ko") -> List[Dict[str, Any]]:
    q = (query or "").strip()
    if not q:
        return []
    host = f"https://{lang}.wikipedia.org"
    search_params = urllib.parse.urlencode(
        {"action": "query", "list": "search", "srsearch": q, "srlimit": "2", "format": "json"}
    )
    raw_s = _http_fetch_bytes(f"{host}/w/api.php?{search_params}", timeout=2.8)
    titles: List[str] = []
    if raw_s:
        try:
            sd = json.loads(raw_s.decode("utf-8"))
            for item in ((sd.get("query") or {}).get("search") or [])[:2]:
                if item.get("title"):
                    titles.append(str(item["title"]))
        except Exception:
            pass
    if not titles:
        titles = [q]

    cards: List[Dict[str, Any]] = []
    for t in titles[:2]:
        enc_t = urllib.parse.quote(t.replace(" ", "_"))
        raw_sum = _http_fetch_bytes(f"{host}/api/rest_v1/page/summary/{enc_t}", timeout=2.6)
        if not raw_sum:
            continue
        try:
            jd = json.loads(raw_sum.decode("utf-8"))
            if jd.get("type") == "disambiguation":
                continue
            extract = _clean_rss_text(str(jd.get("extract") or ""))
            page_url = str(((jd.get("content_urls") or {}).get("desktop") or {}).get("page") or f"{host}/wiki/{enc_t}")
            if extract:
                cards.append(
                    {
                        "title": str(jd.get("title") or t),
                        "source": "한국어 위키백과" if lang == "ko" else "Wikipedia (EN)",
                        "url": page_url,
                        "summary": extract[:240] + ("..." if len(extract) > 240 else ""),
                    }
                )
        except Exception:
            continue
    return cards


def _deduplicate_and_rank_news(
    candidates: List[Dict[str, Any]], q_meta: Dict[str, Any], max_items: int = 26
) -> List[Dict[str, Any]]:
    scored: List[Dict[str, Any]] = []
    seen_urls: Set[str] = set()

    for it in candidates:
        u = (it.get("url") or "").strip()
        t = (it.get("title") or "").strip()
        if not t or (u and u in seen_urls):
            continue
        is_internal = bool(it.get("_is_internal"))
        rel = _score_text_relevance(
            title=t,
            body=str(it.get("snippet") or ""),
            source=str(it.get("source") or ""),
            published_ts=float(it.get("published_ts") or 0.0),
            q_meta=q_meta,
            is_internal_sportiq=is_internal,
        )
        if rel <= 0:
            continue
        if u:
            seen_urls.add(u)
        enriched = {k: v for k, v in it.items() if not k.startswith("_")}
        enriched["match_pct"] = rel
        scored.append(enriched)

    # Sort primarily by precision score bucket, secondarily by recency!
    scored.sort(key=lambda x: (int(x.get("match_pct", 0)) // 4, float(x.get("published_ts") or 0.0)), reverse=True)

    # Jaccard token deduplication (>= 0.68 title token overlap collapsed)
    final_list: List[Dict[str, Any]] = []
    accepted_token_sets: List[Set[str]] = []
    for item in scored:
        toks = {w for w in re.findall(r"[a-z0-9가-힣]{2,}", item["title"].lower()) if len(w) >= 2}
        is_dup = False
        if toks:
            for prev_toks in accepted_token_sets:
                inter = len(toks & prev_toks)
                union = max(1, len(toks | prev_toks))
                if (inter / union) >= 0.68:
                    is_dup = True
                    break
        if is_dup:
            continue
        if toks:
            accepted_token_sets.append(toks)
        final_list.append(item)
        if len(final_list) >= max_items:
            break

    return final_list


def _search_sportiq_deep_internal(q_meta: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    matched_games: List[Dict[str, Any]] = []
    matched_insights: List[Dict[str, Any]] = []
    matched_posts: List[Dict[str, Any]] = []
    cached_news: List[Dict[str, Any]] = []
    seen_matchups: Set[str] = set()

    # 1. Search SportIQ DataCenter: 4-day Live/Upcoming/Finished Matches + Standings + Injuries + Transfers!
    try:
        from app import sportiq_datacenter as _dc

        # 1-A. Live & Scheduled Matches across offsets (0: today, 1: tomorrow, -1: yesterday, 2: +2d)
        raw_matches: List[Dict[str, Any]] = []
        collect_all = getattr(_dc, "_collect_all_matches", None)
        collect_dict = getattr(_dc, "collect_live_scores", None)
        cget_fn = getattr(_dc, "_cget", None)

        for off in (0, 1, -1, 2):
            try:
                if off == 0 and callable(collect_all):
                    res = collect_all(0)
                    if isinstance(res, list):
                        raw_matches.extend(res)
                elif callable(cget_fn):
                    # Check fast cache first for non-zero offsets so search never blocks!
                    c_hit = cget_fn(f"live_scores_{off}", 600.0)
                    if isinstance(c_hit, dict) and isinstance(c_hit.get("items"), list):
                        raw_matches.extend(c_hit["items"])
                    elif off in (0, 1) and callable(collect_dict):
                        d_res = collect_dict(off)
                        if isinstance(d_res, dict) and isinstance(d_res.get("items"), list):
                            raw_matches.extend(d_res["items"])
                elif off == 0 and callable(collect_dict):
                    d_res = collect_dict(0)
                    if isinstance(d_res, dict) and isinstance(d_res.get("items"), list):
                        raw_matches.extend(d_res["items"])
            except Exception:
                continue

        for m in raw_matches:
            if not isinstance(m, dict):
                continue
            m_title = f"{m.get('home', '-')} vs {m.get('away', '-')}"
            m_body = f"{m.get('league', '')} {m.get('sport', '')} {m.get('match_dt_kst', '')} {m.get('status_badge', '')} {m.get('drop_badge', '')}"
            rel = _score_text_relevance(m_title, m_body, "SportIQ Live", time.time(), q_meta, is_internal_sportiq=True)
            if rel <= 0:
                continue
            dedup_k = f"{m.get('league','')}|{m_title.lower()}"
            if dedup_k in seen_matchups:
                continue
            seen_matchups.add(dedup_k)
            ai = m.get("ai") if isinstance(m.get("ai"), dict) else {}
            matched_games.append(
                {
                    "league": str(m.get("league") or "SPORT"),
                    "matchup": m_title,
                    "dt_kst": str(m.get("match_dt_kst") or "일정 확인"),
                    "status": str(m.get("status_badge") or "Scheduled"),
                    "score": f"{m.get('home_score', 0)} : {m.get('away_score', 0)}",
                    "ai_prob": f"홈 {ai.get('home_pct', 50)}% / 원정 {ai.get('away_pct', 50)}%",
                    "drop_badge": str(m.get("drop_badge") or ""),
                    "match_pct": rel,
                }
            )

        # 1-B. Standings Insight (Current League Rank, W-D-L, Points)
        get_st = getattr(_dc, "get_standings", None)
        if callable(get_st):
            try:
                st_data = get_st(q_meta["league_hint"])
                for r in (st_data.get("standings") or []):
                    t_name = str(r.get("team") or "")
                    rel = _score_text_relevance(
                        t_name,
                        f"{q_meta['league_hint']} 순위 승점 standings",
                        "SportIQ Standings",
                        time.time(),
                        q_meta,
                        is_internal_sportiq=True,
                    )
                    if rel >= 68:
                        form_txt = "".join(r.get("form") or [])
                        matched_insights.append(
                            {
                                "kind": "standings",
                                "dc_tab": "standings",
                                "badge": f"📊 {q_meta['league_hint']} 실시간 순위",
                                "title": f"{t_name} — 현재 리그 {r.get('rank')}위 (승점/기록: {r.get('pts_label') or r.get('pts')})",
                                "detail": f"{r.get('gp', 0)}경기 {r.get('w', 0)}승 {r.get('d', 0)}무 {r.get('l', 0)}패 · 득실 {r.get('gd', 0)}" + (f" · 최근폼 {form_txt}" if form_txt else ""),
                                "url": "",
                                "match_pct": rel,
                            }
                        )
                        if len(matched_insights) >= 3:
                            break
            except Exception:
                pass

        # 1-C. Injuries & Medical Report Insights
        get_inj = getattr(_dc, "get_injuries", None)
        if callable(get_inj):
            try:
                inj_data = get_inj(q_meta["league_hint"])
                for ij in (inj_data.get("items") or inj_data.get("injuries") or []):
                    it_title = f"{ij.get('player', '')} ({ij.get('team', '')})"
                    it_body = f"부상 결장 {ij.get('reason', '')} {ij.get('severity', '')} {ij.get('since', '')} injury"
                    rel = _score_text_relevance(it_title, it_body, "SportIQ Medical", time.time(), q_meta, is_internal_sportiq=True)
                    if rel >= 68:
                        matched_insights.append(
                            {
                                "kind": "injuries",
                                "dc_tab": "injuries",
                                "badge": f"🏥 메디컬 · {ij.get('severity', '결장 체크')}",
                                "title": f"{ij.get('team', '')} {ij.get('player', '')} ({ij.get('pos', '-')}) — {ij.get('reason', '')}",
                                "detail": str(ij.get("since") or "구단 메디컬 리포트 확인"),
                                "url": "",
                                "match_pct": rel,
                            }
                        )
                        if len(matched_insights) >= 7:
                            break
            except Exception:
                pass

        # 1-D. Transfers & Player Market Values
        get_tr = getattr(_dc, "get_transfers", None)
        if callable(get_tr):
            try:
                tr_data = get_tr("all", "all")
                for tr in (tr_data.get("feed") or []):
                    rel = _score_text_relevance(
                        str(tr.get("title") or ""),
                        f"이적 {tr.get('cat', '')} {tr.get('ai_brief', '')} transfer",
                        str(tr.get("source") or "SportIQ"),
                        time.time(),
                        q_meta,
                        is_internal_sportiq=True,
                    )
                    if rel >= 70:
                        matched_insights.append(
                            {
                                "kind": "transfers",
                                "dc_tab": "transfers",
                                "badge": f"💼 이적시장 · {tr.get('cat', '소식')}",
                                "title": str(tr.get("title") or ""),
                                "detail": str(tr.get("ai_brief") or ""),
                                "url": str(tr.get("url") or ""),
                                "match_pct": rel,
                            }
                        )
                for mv in (tr_data.get("market_values") or []):
                    mv_title = f"{mv.get('player', '')} ({mv.get('team', '')})"
                    mv_body = f"몸값 시장가치 이적료 {mv.get('league', '')} {mv.get('nation', '')}"
                    rel = _score_text_relevance(mv_title, mv_body, "SportIQ Market", time.time(), q_meta, is_internal_sportiq=True)
                    if rel >= 70:
                        matched_insights.append(
                            {
                                "kind": "market_value",
                                "dc_tab": "transfers",
                                "badge": "💎 선수 몸값(추정 시장가치)",
                                "title": f"{mv.get('player', '')} · {mv.get('team', '')} ({mv.get('pos', '-')})",
                                "detail": f"시장가치 {mv.get('value_label', mv.get('value', '-'))} · 국적: {mv.get('nation', '-')}",
                                "url": "",
                                "match_pct": rel,
                            }
                        )
            except Exception:
                pass
    except Exception:
        pass

    # 2. Search All Relevant SQLite Tables (global_match_rankings, auto_analyst_posts, community_member_posts, overseas_news_cache)
    def _db_deep_lookup() -> None:
        with _db_session(write=False) as conn:
            tables = {
                r[0]
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }

            if "global_match_rankings" in tables:
                rows = conn.execute(
                    "SELECT league, matchup, schedule_venue, votes, buzz_score FROM global_match_rankings ORDER BY votes DESC LIMIT 30"
                ).fetchall()
                for r in rows:
                    rel = _score_text_relevance(
                        r["matchup"],
                        f"{r['league']} {r['schedule_venue']} 빅매치 배당 일정",
                        "SportIQ TOP",
                        time.time(),
                        q_meta,
                        is_internal_sportiq=True,
                    )
                    if rel >= 65:
                        dk = f"{r['league']}|{r['matchup'].lower()}"
                        if dk not in seen_matchups:
                            seen_matchups.add(dk)
                            matched_games.append(
                                {
                                    "league": r["league"],
                                    "matchup": r["matchup"],
                                    "dt_kst": r["schedule_venue"],
                                    "status": f"🔥 화제성 {r['buzz_score']}",
                                    "score": f"팬투표 {r['votes']}표",
                                    "ai_prob": "글로벌 빅매치 등재",
                                    "drop_badge": "",
                                    "match_pct": rel,
                                }
                            )

            # AI Auto-Analyst Posts (Previously omitted from search!)
            if "auto_analyst_posts" in tables:
                cols = {c[1] for c in conn.execute("PRAGMA table_info(auto_analyst_posts)").fetchall()}
                title_col = "title" if "title" in cols else None
                body_col = "body" if "body" in cols else ("content" if "content" in cols else ("summary" if "summary" in cols else None))
                lg_col = "league" if "league" in cols else ("sport" if "sport" in cols else None)
                pick_col = "recommended_pick" if "recommended_pick" in cols else None
                if title_col and body_col:
                    sel_lg = f"COALESCE({lg_col}, 'AI브리핑')" if lg_col else "'AI브리핑'"
                    sel_pk = f"COALESCE({pick_col}, '')" if pick_col else "''"
                    arows = conn.execute(
                        f"SELECT rowid AS rid, {sel_lg} AS lg, {title_col} AS tt, {body_col} AS bd, {sel_pk} AS pk FROM auto_analyst_posts ORDER BY rowid DESC LIMIT 80"
                    ).fetchall()
                    for ar in arows:
                        full_bd = f"{ar['bd'] or ''} {ar['pk'] or ''}"
                        rel = _score_text_relevance(ar["tt"] or "", full_bd, "SportIQ AI 분석실", time.time(), q_meta, is_internal_sportiq=True)
                        if rel >= 66:
                            excerpt = _clean_rss_text(ar["bd"] or "")[:125]
                            if ar["pk"]:
                                excerpt = f"[추천픽: {ar['pk']}] " + excerpt
                            matched_posts.append(
                                {
                                    "id": f"auto_{ar['rid']}",
                                    "league": ar["lg"],
                                    "author": "🤖 SportIQ 프로 분석엔진",
                                    "title": ar["tt"],
                                    "excerpt": excerpt,
                                    "match_pct": rel,
                                }
                            )

            if "community_member_posts" in tables:
                prows = conn.execute(
                    "SELECT id, league, author, title, body, tags_json, created_at FROM community_member_posts ORDER BY created_at DESC LIMIT 100"
                ).fetchall()
                for r in prows:
                    tags_str = " ".join(json.loads(r["tags_json"] or "[]")) if r["tags_json"] else ""
                    rel = _score_text_relevance(
                        r["title"] or "",
                        f"{r['league']} {r['author']} {r['body']} {tags_str}",
                        "SportIQ 라운지",
                        float(r["created_at"] or time.time()),
                        q_meta,
                        is_internal_sportiq=True,
                    )
                    if rel >= 64:
                        matched_posts.append(
                            {
                                "id": r["id"],
                                "league": r["league"],
                                "author": r["author"],
                                "title": r["title"],
                                "excerpt": _clean_rss_text(r["body"] or "")[:125],
                                "match_pct": rel,
                            }
                        )

            if "overseas_news_cache" in tables:
                nrows = conn.execute(
                    "SELECT title, link, source, summary_tag FROM overseas_news_cache ORDER BY rowid DESC LIMIT 150"
                ).fetchall()
                for r in nrows:
                    cached_news.append(
                        {
                            "title": r["title"] or "",
                            "url": r["link"] or "",
                            "source": f"{(r['source'] or '해외속보').upper()} 실시간",
                            "published_kst": r["summary_tag"] or "해외 속보",
                            "published_ts": time.time() - 2400,
                            "snippet": f"SportIQ 15분 자동 수집 글로벌 속보 ({r['summary_tag'] or '원문'})",
                            "lang_badge": "BBC·ESPN",
                            "_is_internal": True,
                        }
                    )

    try:
        run_with_db_retry(_db_deep_lookup)
    except Exception:
        pass

    matched_games.sort(key=lambda x: int(x.get("match_pct", 0)), reverse=True)
    matched_insights.sort(key=lambda x: int(x.get("match_pct", 0)), reverse=True)
    matched_posts.sort(key=lambda x: int(x.get("match_pct", 0)), reverse=True)

    return {
        "games": matched_games[:12],
        "insights": matched_insights[:10],
        "posts": matched_posts[:10],
        "cached_news": cached_news,
    }


def _build_hyper_briefing_summary(
    query: str,
    q_meta: Dict[str, Any],
    web_news: List[Dict[str, Any]],
    knowledge: List[Dict[str, Any]],
    internal: Dict[str, List[Dict[str, Any]]],
) -> str:
    # 1. If Gemini/OpenAI API key is configured on Render, feed enriched multi-source context to LLM
    llm_fn = getattr(_smu, "_try_llm_grounded_summary", None)
    if callable(llm_fn):
        try:
            llm_out = llm_fn(query, web_news, knowledge, internal)
            if llm_out:
                return llm_out
        except Exception:
            pass

    sections: List[str] = []
    if knowledge:
        k0 = knowledge[0]
        sections.append(f"📌 핵심 배경 ({k0['source']}): {k0['summary']}")

    insights = internal.get("insights") or []
    if insights:
        ins_lines = [f"{x['title']} ({x['detail']})" for x in insights[:2]]
        sections.append("📈 구단 순위·메디컬·이적 핵심: " + " / ".join(ins_lines))

    games = internal.get("games") or []
    if games:
        g_lines = [
            f"[{g['league']}] {g['matchup']} ({g['dt_kst']}, {g['status']}) · {g['ai_prob']}"
            + (f" · {g['drop_badge']}" if g.get("drop_badge") else "")
            for g in games[:3]
        ]
        sections.append("⚽ 실시간 경기 & AI 배당 흐름: " + " / ".join(g_lines))

    if web_news:
        headlines = [
            f"• [{n['source']} · 정확도 {n.get('match_pct', 90)}%] {n['title']}"
            for n in web_news[:4]
        ]
        sections.append(
            f"🌐 국내·해외 고신뢰 뉴스 요약 (총 {len(web_news)}건 검증 완료):\n" + "\n".join(headlines)
        )

    posts = internal.get("posts") or []
    if posts:
        sections.append(
            f"✍️ SportIQ 라운지 분석: '{posts[0]['title']}' 등 총 {len(posts)}건의 내부 분석 브리핑이 함께 검색되었습니다."
        )

    if not sections:
        return (
            f"'{query}' 키워드로 국내외 뉴스·백과·데이터센터 10개 채널을 탐색했으나 직결된 스포츠 문서가 적습니다. "
            "구단명·선수명(예: 손흥민, 맨유 부상, 아스널 배당, 오타니, 요키치, 페이커)으로 검색하시면 한·영 자동 확장 결과를 확인하실 수 있습니다."
        )
    return "\n\n".join(sections)


class HyperAiSearchIn(BaseModel):
    query: str
    mode: str = "all"
    nickname: Optional[str] = ""
    pin: Optional[str] = ""


@router.post("/api/ai-iq/search")
def run_hyper_ai_iq_search(payload: HyperAiSearchIn):
    q = re.sub(r"\s+", " ", (payload.query or "").strip())[:100]
    if len(q) < 1:
        raise HTTPException(status_code=400, detail="검색어를 입력해 주세요.")
    mode = (payload.mode or "all").strip().lower()
    if mode not in ("all", "news", "wiki"):
        mode = "all"

    cache_key = f"hyper21:{mode}:{q.lower()}"
    now_m = time.monotonic()
    cached_payload: Optional[Dict[str, Any]] = None
    with _HYPER_SEARCH_LOCK:
        hit = _HYPER_SEARCH_CACHE.get(cache_key)
        if hit and (now_m - hit[0]) <= 90.0:
            cached_payload = dict(hit[1])

    if cached_payload is None:
        q_meta = _analyze_query_concepts(q)
        ko_q = q_meta["ko_primary_q"]
        ko_alt = q_meta["ko_alt_q"]
        en_q = q_meta["en_search_q"]

        ext_news_lists: List[List[Dict[str, Any]]] = []
        ext_know_lists: List[List[Dict[str, Any]]] = []

        with ThreadPoolExecutor(max_workers=8) as pool:
            futs_news = []
            futs_know = []
            if mode in ("all", "news"):
                futs_news.append(pool.submit(_fetch_gnews_rss, ko_q, "ko", 18))
                if ko_alt:
                    futs_news.append(pool.submit(_fetch_gnews_rss, ko_alt, "ko", 10))
                futs_news.append(pool.submit(_fetch_bing_rss, ko_q, "ko-KR", 12))
                if en_q:
                    futs_news.append(pool.submit(_fetch_gnews_rss, en_q, "en", 14))
                    futs_news.append(pool.submit(_fetch_bing_rss, en_q, "en-US", 8))
                    futs_news.append(pool.submit(_fetch_espn_public_search, en_q, 8))

            if mode in ("all", "wiki"):
                w_ko = q_meta.get("wiki_ko_q") or q
                w_en = q_meta.get("wiki_en_q") or ""
                futs_know.append(pool.submit(_fetch_wikipedia_smart, w_ko, "ko"))
                if w_en and w_en.lower() != w_ko.lower():
                    futs_know.append(pool.submit(_fetch_wikipedia_smart, w_en, "en"))

            internal_ctx = _search_sportiq_deep_internal(q_meta)

            for fn in futs_news:
                try:
                    res_n = fn.result(timeout=3.6)
                    if isinstance(res_n, list):
                        ext_news_lists.append(res_n)
                except Exception:
                    continue

            for fk in futs_know:
                try:
                    res_k = fk.result(timeout=3.0)
                    if isinstance(res_k, list):
                        ext_know_lists.append(res_k)
                except Exception:
                    continue

        all_news_candidates: List[Dict[str, Any]] = []
        for nl in ext_news_lists:
            all_news_candidates.extend(nl)
        all_news_candidates.extend(internal_ctx.get("cached_news") or [])

        ranked_news = _deduplicate_and_rank_news(all_news_candidates, q_meta, max_items=28)

        seen_know_titles: Set[str] = set()
        knowledge_cards: List[Dict[str, Any]] = []
        for kl in ext_know_lists:
            for kc in kl:
                kt = (kc.get("title") or "").strip().lower()
                if not kt or kt in seen_know_titles:
                    continue
                if not _is_valid_sports_wiki_card(kc):
                    continue
                seen_know_titles.add(kt)
                knowledge_cards.append(kc)
                if len(knowledge_cards) >= 3:
                    break

        summary_text = _build_hyper_briefing_summary(q, q_meta, ranked_news, knowledge_cards, internal_ctx)

        base_entity = q_meta["stems"][0] if q_meta["stems"] else q
        related_queries = [
            f"{base_entity} 실시간 경기·배당",
            f"{base_entity} 부상·결장 명단",
            f"{base_entity} 리그 순위·승점",
            f"{base_entity} 이적·오피셜 소식",
        ]

        total_matched = (
            len(ranked_news)
            + len(knowledge_cards)
            + len(internal_ctx.get("games") or [])
            + len(internal_ctx.get("insights") or [])
            + len(internal_ctx.get("posts") or [])
        )

        cached_payload = {
            "query": q,
            "mode": mode,
            "searched_at_kst": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
            "ai_summary": summary_text,
            "expanded_terms": q_meta["expanded_terms"],
            "total_count": total_matched,
            "knowledge": knowledge_cards,
            "web_news": ranked_news,
            "sportiq_matches": internal_ctx.get("games", []),
            "sportiq_insights": internal_ctx.get("insights", []),
            "sportiq_posts": internal_ctx.get("posts", []),
            "related_queries": related_queries,
        }
        with _HYPER_SEARCH_LOCK:
            if len(_HYPER_SEARCH_CACHE) > 250:
                _HYPER_SEARCH_CACHE.clear()
            _HYPER_SEARCH_CACHE[cache_key] = (now_m, cached_payload)

    total_cnt = int(cached_payload.get("total_count") or 0)

    def _log_search() -> None:
        with _db_session(write=True) as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_iq_search_logs'"
            ).fetchone()
            if exists:
                conn.execute(
                    """
                    INSERT INTO ai_iq_search_logs(query, norm_query, mode, nickname, result_count, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (q, q.lower(), mode, (payload.nickname or "").strip()[:24], total_cnt, time.time()),
                )

    try:
        run_with_db_retry(_log_search)
        trig_pg = getattr(_smu, "_trigger_pg_backup", None)
        if callable(trig_pg):
            threading.Thread(target=trig_pg, args=("ai_iq_search_logs",), daemon=True).start()
    except Exception:
        pass

    awarded_pts, updated_pts = 0, None
    award_fn = getattr(_smu, "_award_ai_search_bonus_if_eligible", None)
    if callable(award_fn):
        try:
            awarded_pts, updated_pts = award_fn(payload.nickname or "", payload.pin or "", q)
        except Exception:
            pass

    return {
        "ok": True,
        "assistant": "AI 아이큐",
        "engine_version": "2.0-hyper",
        "awarded_points": awarded_pts,
        "updated_points": updated_pts,
        **cached_payload,
    }



# ============================================================================
# SportIQ Member UX v2.2 — Likes/Bookmarks DB + Unified +20P Check-in + Overseas News KO Badges & NFL/UFC/F1
# ============================================================================
_OVERSEAS_V22_CACHE: Tuple[float, List[Dict[str, Any]]] = (0.0, [])
_OVERSEAS_V22_LOCK = threading.Lock()


def _smu_db_session(write: bool = False):
    smu_db = getattr(_smu, "DB_PATH", DB_PATH)
    return managed_sqlite_conn(smu_db, write=write, row_factory=sqlite3.Row)


def _trigger_smu_pg_backup(*tables: str) -> None:
    fn = getattr(_smu, "_trigger_pg_backup", None)
    if callable(fn) and tables:
        try:
            threading.Thread(target=fn, args=tables, daemon=True).start()
        except Exception:
            pass


def _register_ux_v22_pg_tables() -> None:
    extra = ("community_post_likes", "community_post_like_totals", "member_post_bookmarks")
    cur = getattr(_smu, "BACKUP_TABLES", ())
    if isinstance(cur, (tuple, list)):
        merged = tuple(dict.fromkeys(list(cur) + list(extra)))
        _smu.BACKUP_TABLES = merged
    try:
        from app.sportiq_auto_analyst import register_snapshot_table
        register_snapshot_table("community_post_like_totals", "post_id")
    except Exception:
        pass


def _init_ux_v22_tables() -> None:
    _register_ux_v22_pg_tables()

    def _op() -> None:
        now = time.time()
        with _smu_db_session(write=True) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS community_post_likes (
                    post_id TEXT NOT NULL,
                    actor_key TEXT NOT NULL,
                    author_rewarded INTEGER NOT NULL DEFAULT 0,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (post_id, actor_key)
                );
                CREATE TABLE IF NOT EXISTS community_post_like_totals (
                    post_id TEXT PRIMARY KEY,
                    like_count INTEGER NOT NULL DEFAULT 1,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS member_post_bookmarks (
                    actor_key TEXT NOT NULL,
                    post_id TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    league TEXT NOT NULL DEFAULT '',
                    author TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    PRIMARY KEY (actor_key, post_id)
                );
                """
            )
            has_cmp = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='community_member_posts'"
            ).fetchone()
            if has_cmp:
                cnt = int(conn.execute("SELECT COUNT(*) AS c FROM community_member_posts").fetchone()["c"] or 0)
                if cnt == 0:
                    seeds = [
                        (
                            "mp_seed_epl_nwd",
                            "EPL",
                            "안필드전술가",
                            "🎯 데이터 저격수",
                            1,
                            94,
                            "[전술 프리뷰] 리버풀 vs 맨체스터 유나이티드 — 우측 하프스페이스 압박과 전환 속도 승부처",
                            "안필드 노스웨스트 더비 핵심은 리버풀 우측 공격 전개(살라-트렌트 연계)와 맨유 아모림 체제 스리백 스토퍼의 전진 수비 간격입니다. 최근 5경기 기대득점(xG) 흐름상 전반 25분 이내 첫 슈팅 볼륨 우위 팀의 승률이 72%에 달해 초반 중원 압박 강도가 승부를 가를 전망입니다.",
                            json.dumps(["#EPL", "#리버풀", "#맨유", "#xG전술분석"], ensure_ascii=False),
                            14,
                            "",
                            now - 1800,
                            now - 1800,
                        ),
                        (
                            "mp_seed_nba_bos_dal",
                            "NBA",
                            "코트비전PRO",
                            "🔥 연승 전술가",
                            1,
                            92,
                            "[데이터 리포트] 보스턴 셀틱스 vs 댈러스 매버릭스 — 코너 3점 억제율과 클러치 오펜시브 레이팅 비교",
                            "보스턴의 파이브아웃 스페이싱은 경기당 3점 시도 1위를 유지 중이며, 댈러스는 돈치치 픽앤롤 이후 약측 코너 킥아웃 효율이 핵심입니다. 4쿼터 클러치 구간 수비 리바운드 확보율에서 앞서는 팀이 핸디캡 우위를 점할 가능성이 높습니다.",
                            json.dumps(["#NBA", "#보스턴셀틱스", "#댈러스", "#클러치분석"], ensure_ascii=False),
                            11,
                            "",
                            now - 3900,
                            now - 3900,
                        ),
                        (
                            "mp_seed_kbo_kia",
                            "KBO",
                            "세이버마스터",
                            "⚡ 드롭오즈 헌터",
                            0,
                            91,
                            "[스토브리그 분석] KIA 타이거즈 투타 WAR 보존율과 내년 시즌 상위 타선 출루 시너지 점검",
                            "통합 우승 전력의 핵심이던 내야 공수 밸런스와 좌우 불펜 필승조 이닝 분담률을 세이버메트릭스 지표로 재점검했습니다. 주축 타선의 컨택률과 장타율 편차가 적어 장기 레이스 연패 억제력이 여전히 리그 최상위권으로 평가됩니다.",
                            json.dumps(["#KBO", "#KIA타이거즈", "#김도영", "#세이버메트릭스"], ensure_ascii=False),
                            9,
                            "",
                            now - 6400,
                            now - 6400,
                        ),
                    ]
                    conn.executemany(
                        """
                        INSERT OR IGNORE INTO community_member_posts(
                            id, league, author, author_badge, is_pro, trust, title, body, tags_json, likes, pin_hash, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        seeds,
                    )
                    for sid, _lg, _au, _bg, _pr, _tr, _ti, _bo, _tg, lk, _ph, _ca, _ua in seeds:
                        conn.execute(
                            "INSERT OR IGNORE INTO community_post_like_totals(post_id, like_count, updated_at) VALUES (?, ?, ?)",
                            (sid, lk, now),
                        )

    try:
        run_with_db_retry(_op)
    except Exception:
        pass


_init_ux_v22_tables()


def _normalize_actor_keys(actor_key: Optional[str] = "", nickname: Optional[str] = "") -> Tuple[str, List[str]]:
    clean_nick = (nickname or "").strip()[:24]
    clean_actor = re.sub(r"[^a-zA-Z0-9_:\-.가-힣]", "", (actor_key or "").strip())[:72]
    keys: List[str] = []
    if len(clean_nick) >= 2:
        keys.append(f"member:{clean_nick.lower()}")
    if clean_actor:
        keys.append(clean_actor)
    if not keys:
        keys.append("anon:default")
    primary = keys[0]
    return primary, list(dict.fromkeys(keys))


class PostReactionIn(BaseModel):
    actor_key: Optional[str] = ""
    nickname: Optional[str] = ""
    base_likes: Optional[int] = 1
    title: Optional[str] = ""
    league: Optional[str] = ""
    author: Optional[str] = ""


class LoungeCheckinIn(BaseModel):
    nickname: Optional[str] = ""
    pin: Optional[str] = ""
    actor_key: Optional[str] = ""
    client_points: Optional[int] = 120
    client_streak: Optional[int] = 0



@router.get("/api/member/p0p1-upgrade.js")
def serve_patched_upgrade_js_v22():
    from fastapi.responses import Response
    _ensure_upgrade_js_dedup_patched()
    body = getattr(_smu, "UPGRADE_JS", "")
    return Response(
        content=body,
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": "no-cache, max-age=0, must-revalidate"},
    )


@router.get("/api/posts")
def list_community_posts_v22(actor_key: Optional[str] = "", nickname: Optional[str] = ""):
    _primary, actor_keys = _normalize_actor_keys(actor_key, nickname)

    def _op() -> Dict[str, Any]:
        with _smu_db_session(write=False) as conn:
            tables = {
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            p_rows = (
                conn.execute("SELECT * FROM community_member_posts ORDER BY created_at DESC LIMIT 60").fetchall()
                if "community_member_posts" in tables
                else []
            )
            c_rows = (
                conn.execute(
                    "SELECT post_id, user_name, user_badge, comment_text, created_at FROM community_post_comments ORDER BY id ASC"
                ).fetchall()
                if "community_post_comments" in tables
                else []
            )
            tot_rows = (
                conn.execute("SELECT post_id, like_count FROM community_post_like_totals").fetchall()
                if "community_post_like_totals" in tables
                else []
            )
            like_counts: Dict[str, int] = {r["post_id"]: int(r["like_count"] or 0) for r in tot_rows}

            placeholders = ",".join("?" for _ in actor_keys)
            liked_ids: Set[str] = set()
            if "community_post_likes" in tables and actor_keys:
                lrows = conn.execute(
                    f"SELECT post_id FROM community_post_likes WHERE is_active = 1 AND actor_key IN ({placeholders})",
                    tuple(actor_keys),
                ).fetchall()
                liked_ids = {r["post_id"] for r in lrows}

            bookmarked_ids: Set[str] = set()
            if "member_post_bookmarks" in tables and actor_keys:
                brows = conn.execute(
                    f"SELECT post_id FROM member_post_bookmarks WHERE actor_key IN ({placeholders})",
                    tuple(actor_keys),
                ).fetchall()
                bookmarked_ids = {r["post_id"] for r in brows}

            auto_rows = []
            if "auto_analyst_posts" in tables:
                try:
                    auto_rows = conn.execute(
                        "SELECT * FROM auto_analyst_posts ORDER BY rowid DESC LIMIT 12"
                    ).fetchall()
                except Exception:
                    auto_rows = []

        comments_by_post: Dict[str, List[Dict[str, str]]] = {}
        for c in c_rows:
            label = f"{c['user_name']} {c['user_badge']}".strip()
            comments_by_post.setdefault(c["post_id"], []).append({"user": label, "text": c["comment_text"]})

        now = time.time()
        posts: List[Dict[str, Any]] = []
        seen_ids: Set[str] = set()

        for r in p_rows:
            pid = r["id"]
            seen_ids.add(pid)
            mins = max(0, int((now - float(r["created_at"] or now)) // 60))
            base_lk = int(r["likes"] or 1)
            eff_likes = like_counts.get(pid, base_lk)
            like_counts[pid] = eff_likes
            posts.append(
                {
                    "id": pid,
                    "league": r["league"],
                    "isPro": bool(r["is_pro"]),
                    "author": r["author"],
                    "authorBadge": r["author_badge"] or "",
                    "trust": int(r["trust"] or 90),
                    "likes": eff_likes,
                    "bookmarked": pid in bookmarked_ids,
                    "liked": pid in liked_ids,
                    "hidden": False,
                    "time": mins,
                    "title": r["title"],
                    "body": r["body"],
                    "tags": json.loads(r["tags_json"] or "[]"),
                    "comments": comments_by_post.get(pid, []),
                    "isMemberServerPost": True,
                    "hasPin": bool(r["pin_hash"]),
                }
            )

        for idx, ar in enumerate(auto_rows):
            ad = dict(ar)
            aid = str(ad.get("id") or ad.get("post_id") or f"ai_auto_{idx}")
            if not aid.startswith("ai_"):
                aid = f"ai_{aid}"
            if aid in seen_ids:
                continue
            seen_ids.add(aid)
            title = str(ad.get("title") or "").strip()
            body = str(ad.get("body") or ad.get("content") or ad.get("summary") or "").strip()
            if not title or not body:
                continue
            lg = str(ad.get("league") or ad.get("sport") or "AI 데이터").strip()[:20]
            cts = _to_epoch_ts(ad.get("created_at")) or (now - (idx + 1) * 900)
            mins = max(1, int((now - cts) // 60))
            eff_likes = like_counts.get(aid, int(ad.get("likes") or 6))
            like_counts[aid] = eff_likes
            posts.append(
                {
                    "id": aid,
                    "league": lg,
                    "isPro": False,
                    "isAiBriefing": True,
                    "isAi": True,
                    "isAiPost": True,
                    "author": str(ad.get("author") or "🤖 SportIQ AI 데이터분석팀"),
                    "authorBadge": "🤖 AI 공식브리핑",
                    "trust": 95,
                    "likes": eff_likes,
                    "bookmarked": aid in bookmarked_ids,
                    "liked": aid in liked_ids,
                    "hidden": False,
                    "time": mins,
                    "title": title,
                    "body": body,
                    "tags": [f"#{lg.replace(' ', '')}", "#AI브리핑", "#데이터분석"],
                    "comments": comments_by_post.get(aid, []),
                    "isMemberServerPost": False,
                    "hasPin": False,
                }
            )

        return {
            "ok": True,
            "posts": posts,
            "extra_comments": comments_by_post,
            "like_counts": like_counts,
            "liked_ids": sorted(liked_ids),
            "bookmarked_ids": sorted(bookmarked_ids),
        }

    return run_with_db_retry(_op)


@router.get("/api/posts/reactions")
def get_post_reactions_v22(actor_key: Optional[str] = "", nickname: Optional[str] = ""):
    _primary, actor_keys = _normalize_actor_keys(actor_key, nickname)

    def _op() -> Dict[str, Any]:
        with _smu_db_session(write=False) as conn:
            tot_rows = conn.execute("SELECT post_id, like_count FROM community_post_like_totals").fetchall()
            like_counts = {r["post_id"]: int(r["like_count"] or 0) for r in tot_rows}
            placeholders = ",".join("?" for _ in actor_keys)
            lrows = conn.execute(
                f"SELECT post_id FROM community_post_likes WHERE is_active = 1 AND actor_key IN ({placeholders})",
                tuple(actor_keys),
            ).fetchall()
            brows = conn.execute(
                f"SELECT post_id, title, league, author, created_at FROM member_post_bookmarks WHERE actor_key IN ({placeholders}) ORDER BY created_at DESC",
                tuple(actor_keys),
            ).fetchall()
            return {
                "ok": True,
                "like_counts": like_counts,
                "liked_ids": [r["post_id"] for r in lrows],
                "bookmarked_ids": [r["post_id"] for r in brows],
                "bookmarks": [dict(r) for r in brows],
            }

    return run_with_db_retry(_op)


@router.post("/api/posts/{post_id}/like")
def toggle_post_like_v22(post_id: str, payload: Optional[PostReactionIn] = None):
    pid = (post_id or "").strip()[:80]
    if not pid:
        raise HTTPException(status_code=400, detail="유효하지 않은 게시글 ID입니다.")
    primary_actor, actor_keys = _normalize_actor_keys(
        payload.actor_key if payload else "",
        payload.nickname if payload else "",
    )
    base_likes = max(0, int((payload.base_likes if payload and payload.base_likes is not None else 1) or 0))
    liker_nick = ((payload.nickname if payload else "") or "").strip().lower()
    now = time.time()

    def _op() -> Dict[str, Any]:
        author_bonus = 0
        with _smu_db_session(write=True) as conn:
            cmp_row = conn.execute(
                "SELECT id, author, likes FROM community_member_posts WHERE id = ?",
                (pid,),
            ).fetchone()
            tot_row = conn.execute(
                "SELECT like_count FROM community_post_like_totals WHERE post_id = ?",
                (pid,),
            ).fetchone()
            cur_total = (
                int(tot_row["like_count"])
                if tot_row is not None
                else (int(cmp_row["likes"]) if cmp_row is not None else base_likes)
            )

            placeholders = ",".join("?" for _ in actor_keys)
            existing = conn.execute(
                f"SELECT actor_key, is_active, author_rewarded FROM community_post_likes WHERE post_id = ? AND actor_key IN ({placeholders}) ORDER BY is_active DESC LIMIT 1",
                (pid, *actor_keys),
            ).fetchone()

            if existing and int(existing["is_active"]) == 1:
                conn.execute(
                    f"UPDATE community_post_likes SET is_active = 0, updated_at = ? WHERE post_id = ? AND actor_key IN ({placeholders})",
                    (now, pid, *actor_keys),
                )
                new_liked = False
                new_total = max(0, cur_total - 1)
            else:
                already_rewarded = int(existing["author_rewarded"]) if existing else 0
                target_actor = existing["actor_key"] if existing else primary_actor
                if cmp_row and not already_rewarded:
                    post_author = (cmp_row["author"] or "").strip()
                    if post_author and post_author.lower() != liker_nick:
                        acc = conn.execute(
                            "SELECT nickname, points FROM member_accounts WHERE lower(nickname) = lower(?)",
                            (post_author,),
                        ).fetchone()
                        if acc:
                            new_pts = int(acc["points"] or 0) + 2
                            tier_fn = getattr(_smu, "_tier_for_points", lambda p: "PRO" if p >= 350 else ("MEMBER" if p >= 150 else "GUEST"))
                            conn.execute(
                                "UPDATE member_accounts SET points = ?, role_tier = ?, updated_at = ? WHERE nickname = ?",
                                (new_pts, tier_fn(new_pts), now, acc["nickname"]),
                            )
                            author_bonus = 2
                    already_rewarded = 1

                conn.execute(
                    """
                    INSERT INTO community_post_likes(post_id, actor_key, author_rewarded, is_active, updated_at)
                    VALUES (?, ?, ?, 1, ?)
                    ON CONFLICT(post_id, actor_key) DO UPDATE SET
                        is_active = 1,
                        author_rewarded = MAX(community_post_likes.author_rewarded, excluded.author_rewarded),
                        updated_at = excluded.updated_at
                    """,
                    (pid, target_actor, already_rewarded, now),
                )
                new_liked = True
                new_total = cur_total + 1

            conn.execute(
                """
                INSERT INTO community_post_like_totals(post_id, like_count, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(post_id) DO UPDATE SET
                    like_count = excluded.like_count,
                    updated_at = excluded.updated_at
                """,
                (pid, new_total, now),
            )
            if cmp_row is not None:
                conn.execute(
                    "UPDATE community_member_posts SET likes = ?, updated_at = ? WHERE id = ?",
                    (new_total, now, pid),
                )
            return {
                "ok": True,
                "post_id": pid,
                "liked": new_liked,
                "likes": new_total,
                "author_rewarded_points": author_bonus,
            }

    res = run_with_db_retry(_op)
    backup_targets = ["community_post_likes", "community_post_like_totals", "community_member_posts"]
    if res.get("author_rewarded_points"):
        backup_targets.append("member_accounts")
    _trigger_smu_pg_backup(*backup_targets)
    return res


@router.post("/api/posts/{post_id}/bookmark")
def toggle_post_bookmark_v22(post_id: str, payload: Optional[PostReactionIn] = None):
    pid = (post_id or "").strip()[:80]
    if not pid:
        raise HTTPException(status_code=400, detail="유효하지 않은 게시글 ID입니다.")
    primary_actor, actor_keys = _normalize_actor_keys(
        payload.actor_key if payload else "",
        payload.nickname if payload else "",
    )
    title = ((payload.title if payload else "") or "").strip()[:160]
    league = ((payload.league if payload else "") or "").strip()[:24]
    author = ((payload.author if payload else "") or "").strip()[:32]
    now = time.time()

    def _op() -> Dict[str, Any]:
        with _smu_db_session(write=True) as conn:
            placeholders = ",".join("?" for _ in actor_keys)
            existing = conn.execute(
                f"SELECT actor_key FROM member_post_bookmarks WHERE post_id = ? AND actor_key IN ({placeholders}) LIMIT 1",
                (pid, *actor_keys),
            ).fetchone()
            if existing:
                conn.execute(
                    f"DELETE FROM member_post_bookmarks WHERE post_id = ? AND actor_key IN ({placeholders})",
                    (pid, *actor_keys),
                )
                bookmarked = False
            else:
                if not title:
                    cmp_row = conn.execute(
                        "SELECT title, league, author FROM community_member_posts WHERE id = ?",
                        (pid,),
                    ).fetchone()
                    if cmp_row:
                        nonlocal_title = cmp_row["title"] or ""
                        nonlocal_league = cmp_row["league"] or ""
                        nonlocal_author = cmp_row["author"] or ""
                    else:
                        nonlocal_title, nonlocal_league, nonlocal_author = pid, "라운지", "SportIQ"
                else:
                    nonlocal_title, nonlocal_league, nonlocal_author = title, league, author
                conn.execute(
                    """
                    INSERT OR REPLACE INTO member_post_bookmarks(actor_key, post_id, title, league, author, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (primary_actor, pid, nonlocal_title, nonlocal_league, nonlocal_author, now),
                )
                bookmarked = True

            brows = conn.execute(
                f"SELECT post_id FROM member_post_bookmarks WHERE actor_key IN ({placeholders}) ORDER BY created_at DESC",
                tuple(actor_keys),
            ).fetchall()
            return {
                "ok": True,
                "post_id": pid,
                "bookmarked": bookmarked,
                "bookmarked_ids": [r["post_id"] for r in brows],
                "total_bookmarks": len(brows),
            }

    res = run_with_db_retry(_op)
    _trigger_smu_pg_backup("member_post_bookmarks")
    return res


@router.post("/api/member/checkin")
def lounge_unified_daily_checkin_v22(payload: LoungeCheckinIn):
    today_kst = datetime.now(KST).strftime("%Y-%m-%d")
    yesterday_kst = (datetime.now(KST) - timedelta(days=1)).strftime("%Y-%m-%d")
    nick = (payload.nickname or "").strip()[:24]
    pin = re.sub(r"\D", "", payload.pin or "")
    primary_actor, _ = _normalize_actor_keys(payload.actor_key, nick)
    now = time.time()
    tier_fn = getattr(
        _smu,
        "_tier_for_points",
        lambda p: "PRO" if p >= 350 else ("MEMBER" if p >= 150 else "GUEST"),
    )
    hash_pin_fn = getattr(_smu, "_hash_pin", None)

    if len(nick) >= 2 and len(pin) == 4 and callable(hash_pin_fn):
        pin_h = hash_pin_fn(nick, pin)

        def _member_op() -> Dict[str, Any]:
            with _smu_db_session(write=True) as conn:
                row = conn.execute(
                    "SELECT * FROM member_accounts WHERE lower(nickname) = lower(?)",
                    (nick,),
                ).fetchone()
                if row is None:
                    base_pts = max(120, min(int(payload.client_points or 120), 5000))
                    bonus = 20
                    new_pts = base_pts + bonus
                    new_tier = tier_fn(new_pts)
                    conn.execute(
                        """
                        INSERT INTO member_accounts(
                            nickname, pin_hash, points, role_tier, streak_days,
                            last_checkin_date, active_badge, owned_items_json, my_teams_json, updated_at
                        ) VALUES (?, ?, ?, ?, 1, ?, '', '[]', '[]', ?)
                        """,
                        (nick, pin_h, new_pts, new_tier, today_kst, now),
                    )
                    conn.execute(
                        """
                        INSERT INTO wp_point_ledger(nickname, event_type, ref_id, points_delta, kst_date, site_origin, created_at)
                        VALUES (?, 'daily_checkin', ?, ?, ?, 'open_lounge', ?)
                        """,
                        (nick, today_kst, bonus, today_kst, now),
                    )
                    return {
                        "ok": True,
                        "already_checked": False,
                        "awarded_points": bonus,
                        "points": new_pts,
                        "role_tier": new_tier,
                        "streak_days": 1,
                        "last_checkin_date": today_kst,
                        "nickname": nick,
                        "synced_member": True,
                        "message": f"출석 체크 완료! +{bonus}P가 통합 계정에 영구 적립되었습니다. (연속 1일째)",
                    }

                if row["pin_hash"] != pin_h:
                    raise HTTPException(status_code=403, detail="4자리 PIN 번호가 일치하지 않습니다.")

                if (row["last_checkin_date"] or "") == today_kst:
                    pts = int(row["points"] or 120)
                    return {
                        "ok": True,
                        "already_checked": True,
                        "awarded_points": 0,
                        "points": pts,
                        "role_tier": tier_fn(pts),
                        "streak_days": int(row["streak_days"] or 1),
                        "last_checkin_date": today_kst,
                        "nickname": row["nickname"],
                        "synced_member": True,
                        "message": "오늘은 이미 출석 체크를 완료하셨습니다. (라운지·워드프레스 통합 1일 1회)",
                    }

                prev_streak = int(row["streak_days"] or 0)
                new_streak = (prev_streak + 1) if (row["last_checkin_date"] == yesterday_kst) else 1
                bonus = 20 + (10 if new_streak > 0 and new_streak % 7 == 0 else 0)
                new_pts = int(row["points"] or 120) + bonus
                new_tier = tier_fn(new_pts)
                conn.execute(
                    """
                    UPDATE member_accounts
                    SET points = ?, role_tier = ?, streak_days = ?, last_checkin_date = ?, updated_at = ?
                    WHERE nickname = ?
                    """,
                    (new_pts, new_tier, new_streak, today_kst, now, row["nickname"]),
                )
                conn.execute(
                    """
                    INSERT INTO wp_point_ledger(nickname, event_type, ref_id, points_delta, kst_date, site_origin, created_at)
                    VALUES (?, 'daily_checkin', ?, ?, ?, 'open_lounge', ?)
                    """,
                    (row["nickname"], today_kst, bonus, today_kst, now),
                )
                return {
                    "ok": True,
                    "already_checked": False,
                    "awarded_points": bonus,
                    "points": new_pts,
                    "role_tier": new_tier,
                    "streak_days": new_streak,
                    "last_checkin_date": today_kst,
                    "nickname": row["nickname"],
                    "synced_member": True,
                    "message": f"출석 체크 완료! +{bonus}P 통합 적립 (연속 {new_streak}일째 · 현재 {new_pts}P)",
                }

        res = run_with_db_retry(_member_op)
        if not res.get("already_checked"):
            _trigger_smu_pg_backup("member_accounts", "wp_point_ledger")
        return res

    # Unlinked Guest Browser Check-in (Unified +20P reward + DB ledger dedup by actor_key)
    def _guest_op() -> Dict[str, Any]:
        with _smu_db_session(write=True) as conn:
            dup = conn.execute(
                "SELECT id FROM wp_point_ledger WHERE nickname = ? AND event_type = 'guest_checkin' AND kst_date = ? LIMIT 1",
                (primary_actor, today_kst),
            ).fetchone()
            c_pts = max(120, int(payload.client_points or 120))
            c_streak = max(0, int(payload.client_streak or 0))
            if dup:
                return {
                    "ok": True,
                    "already_checked": True,
                    "awarded_points": 0,
                    "points": c_pts,
                    "role_tier": tier_fn(c_pts),
                    "streak_days": max(1, c_streak),
                    "last_checkin_date": today_kst,
                    "synced_member": False,
                    "message": "오늘은 이미 출석 체크(+20P)를 완료했습니다.",
                }
            new_streak = c_streak + 1
            bonus = 20 + (10 if new_streak > 0 and new_streak % 7 == 0 else 0)
            new_pts = c_pts + bonus
            conn.execute(
                """
                INSERT INTO wp_point_ledger(nickname, event_type, ref_id, points_delta, kst_date, site_origin, created_at)
                VALUES (?, 'guest_checkin', ?, ?, ?, 'open_lounge_guest', ?)
                """,
                (primary_actor, today_kst, bonus, today_kst, now),
            )
            return {
                "ok": True,
                "already_checked": False,
                "awarded_points": bonus,
                "points": new_pts,
                "role_tier": tier_fn(new_pts),
                "streak_days": new_streak,
                "last_checkin_date": today_kst,
                "synced_member": False,
                "message": f"출석 체크 완료! +{bonus}P 적립 (상단 🔐 PIN 연동 시 기기 간 영구 보관됩니다)",
            }

    return run_with_db_retry(_guest_op)


@router.get("/api/member/live-ranking")
def get_live_member_point_ranking_v22():
    tier_fn = getattr(
        _smu,
        "_tier_for_points",
        lambda p: "PRO" if p >= 350 else ("MEMBER" if p >= 150 else "GUEST"),
    )

    def _op() -> List[Dict[str, Any]]:
        with _smu_db_session(write=False) as conn:
            has_tbl = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='member_accounts'"
            ).fetchone()
            if not has_tbl:
                return []
            rows = conn.execute(
                "SELECT nickname, points, role_tier, active_badge, streak_days FROM member_accounts ORDER BY points DESC, streak_days DESC, updated_at DESC LIMIT 30"
            ).fetchall()
            out = []
            for idx, r in enumerate(rows):
                pts = int(r["points"] or 120)
                out.append(
                    {
                        "rank": idx + 1,
                        "nickname": r["nickname"],
                        "points": pts,
                        "role_tier": tier_fn(pts),
                        "active_badge": r["active_badge"] or "",
                        "streak_days": int(r["streak_days"] or 0),
                    }
                )
            return out

    return {"ok": True, "members": run_with_db_retry(_op)}


# --- Overseas Sports News Korean Badge / 1-Line Summary Classifier & NFL/UFC/F1 Live Collector ---
_OVERSEAS_ENTITY_KO_MAP: Tuple[Tuple[Tuple[str, ...], str], ...] = (
    # Soccer Clubs & Stars
    (("manchester united", "man utd", "man united", "amorim"), "맨유"),
    (("manchester city", "man city", "haaland", "guardiola"), "맨시티"),
    (("liverpool", "salah", "anfield", "arne slot"), "리버풀"),
    (("arsenal", "saka", "arteta"), "아스널"),
    (("tottenham", "spurs", "son heung-min", "heung-min son"), "토트넘·손흥민"),
    (("chelsea", "cole palmer"), "첼시"),
    (("newcastle",), "뉴캐슬"),
    (("real madrid", "mbappe", "vinicius", "bellingham"), "레알 마드리드"),
    (("barcelona", "yamal", "lewandowski", "flick"), "바르셀로나"),
    (("bayern", "harry kane", "kim min-jae"), "바이에른 뮌헨"),
    (("psg", "paris saint-germain", "lee kang-in"), "PSG"),
    (("inter milan", "internazionale"), "인터 밀란"),
    (("juventus",), "유벤투스"),
    (("argentina", "messi", "alvarez", "álvarez", "nico paz"), "아르헨티나"),
    (("portugal", "ronaldo", "forbs"), "포르투갈·호날두"),
    (("england", "tuchel"), "잉글랜드"),
    (("spain",), "스페인"),
    (("brazil",), "브라질"),
    (("france",), "프랑스"),
    (("womens footballer", "women's footballer"), "올해의 여자축구상"),
    # NBA
    (("lakers", "lebron", "anthony davis"), "LA 레이커스"),
    (("warriors", "stephen curry", "curry"), "골든스테이트·커리"),
    (("celtics", "tatum"), "보스턴 셀틱스"),
    (("mavericks", "mavs", "doncic"), "댈러스·돈치치"),
    (("nuggets", "jokic"), "덴버·요키치"),
    (("suns", "durant"), "피닉스 선즈"),
    (("bucks", "giannis"), "밀워키·야니스"),
    (("blazers", "trail blazers", "avdija"), "포틀랜드 블레이저스"),
    (("timberwolves", "anthony edwards"), "미네소타 팀버울브스"),
    (("knicks",), "뉴욕 닉스"),
    (("thunder", "shai"), "오클라호마시티"),
    (("spurs", "wembanyama"), "샌안토니오·웸반야마"),
    # MLB
    (("dodgers", "ohtani"), "다저스·오타니"),
    (("yankees", "aaron judge", "juan soto"), "양키스·저지"),
    (("mets",), "뉴욕 메츠"),
    (("padres",), "샌디에이고"),
    (("phillies",), "필라델피아"),
    (("braves",), "애틀랜타"),
    # NHL
    (("oilers", "mcdavid"), "에드먼턴·맥데이비드"),
    (("panthers",), "플로리다 팬서스"),
    (("maple leafs", "matthews"), "토론토 메이플리프스"),
    (("rangers",), "뉴욕 레인저스"),
    (("avalanche", "mackinnon"), "콜로라도"),
    (("capitals", "ovechkin"), "워싱턴·오베치킨"),
    # NFL
    (("chiefs", "mahomes", "kelce"), "캔자스시티·마홈스"),
    (("lions", "jared goff"), "디트로이트 라이온스"),
    (("bills", "josh allen"), "버팔로 빌스"),
    (("ravens", "lamar jackson"), "볼티모어·라마 잭슨"),
    (("eagles", "saquon", "jalen hurts"), "필라델피아 이글스"),
    (("49ers", "niners", "purdy", "mccaffrey"), "샌프란시스코 49ers"),
    (("cowboys", "dak prescott"), "댈러스 카우보이스"),
    (("steelers", "russell wilson"), "피츠버그 스틸러스"),
    (("packers", "jordan love"), "그린베이 패커스"),
    (("bengals", "joe burrow"), "신시내티 벵골스"),
    (("texans", "stroud"), "휴스턴 텍산스"),
    (("commanders", "jayden daniels"), "워싱턴 커맨더스"),
    (("patriots",), "뉴잉글랜드 패트리어츠"),
    (("jets", "aaron rodgers"), "뉴욕 제츠"),
    # UFC / MMA
    (("jon jones", "stipe miocic"), "존 존스·헤비급"),
    (("alex pereira", "pereira"), "알렉스 페레이라"),
    (("ilia topuria", "topuria"), "일리아 토푸리아"),
    (("islam makhachev", "makhachev"), "이슬람 마카체프"),
    (("khamzat", "chimaev"), "함자트 치마예프"),
    (("conor mcgregor", "mcgregor"), "코너 맥그리거"),
    (("sean o'malley", "merab"), "밴텀급 타이틀전선"),
    (("dricus", "du plessis", "strickland"), "미들급 타이틀전선"),
    (("dana white",), "데이나 화이트·UFC"),
    # F1
    (("verstappen", "red bull"), "막스 베르스타펜·레드불"),
    (("lando norris", "norris", "piastri", "mclaren"), "맥라렌·노리스"),
    (("leclerc", "ferrari", "sainz"), "페라리·르클레르"),
    (("hamilton", "russell", "mercedes"), "메르세데스·해밀턴"),
    (("las vegas grand prix", "las vegas gp", "vegas gp"), "라스베이거스 GP"),
    (("qatar grand prix", "abu dhabi"), "중동 파이널 GP"),
    (("alonso", "aston martin"), "애스턴마틴·알론소"),
)

_SPORT_META_KO: Dict[str, Tuple[str, str]] = {
    "축구": ("⚽ 해외축구", "#38bdf8"),
    "야구": ("⚾ MLB·야구", "#34d399"),
    "농구": ("🏀 NBA·농구", "#fb923c"),
    "하키": ("🏒 NHL·하키", "#22d3ee"),
    "NFL": ("🏈 NFL 미식축구", "#a78bfa"),
    "UFC": ("🥊 UFC·격투기", "#f43f5e"),
    "F1": ("🏎️ F1 포뮬러원", "#facc15"),
}


def _detect_overseas_sport_ko(title: str, source: str = "", raw_sport: str = "") -> str:
    low = f"{title or ''} {source or ''} {raw_sport or ''}".lower()
    rs = (raw_sport or "").strip()
    if rs in ("NFL", "UFC", "F1", "축구", "야구", "농구", "하키"):
        return rs
    if any(k in low for k in ("formula 1", "formula one", " f1 ", "espn f1", "bbc f1", "grand prix", "verstappen", "mclaren", "leclerc", "ferrari f1", "paddock", "qualifying")):
        return "F1"
    if any(k in low for k in ("ufc", "mma", "octagon", "bellator", "jon jones", "pereira", "topuria", "makhachev", "dana white", "heavyweight title", "knockout")):
        return "UFC"
    if any(k in low for k in ("nfl", "quarterback", "touchdown", "super bowl", "chiefs", "mahomes", "bills", "ravens", "49ers", "cowboys", "steelers", "packers", "eagles", "bengals", "texans", "patriots", "seahawks", "broncos", "chargers")):
        return "NFL"
    if any(k in low for k in ("nhl", "stanley cup", "hockey", "oilers", "mcdavid", "maple leafs", "panthers", "bruins", "rangers", "avalanche", "ovechkin")):
        return "하키"
    if any(k in low for k in ("nba", "basketball", "lakers", "celtics", "warriors", "mavericks", "nuggets", "blazers", "timberwolves", "knicks", "bucks", "suns", "thunder", "wembanyama", "jokic", "lebron", "curry")):
        return "농구"
    if any(k in low for k in ("mlb", "baseball", "dodgers", "yankees", "ohtani", "mets", "padres", "phillies", "braves", "red sox", "home run", "cy young", "silver slugger")):
        return "야구"
    return "축구"


def _detect_overseas_topic_ko(title: str) -> Tuple[str, str, str]:
    low = (title or "").lower()
    if any(k in low for k in ("rule", "out ", "out vs", "injur", "sore", "surgery", "hamstring", "knee", "concussion", "doubtful", "sidelined", "miss ", "return from", "sprain", "fracture")):
        return ("🚑 부상·결장", "#fca5a5", "부상·결장 및 로스터 컨디션 소식")
    if any(k in low for k in ("transfer", "sign", "deal", "contract", "trade", "free agent", "extension", "loan", "exit", "target", "acquire", "waive", "release")):
        return ("🔁 이적·계약", "#c4b5fd", "선수 영입·트레이드·재계약 동향")
    if any(k in low for k in ("score", "rout", "win", "beat", "defeat", "victory", "goal", "double", "hat-trick", "knockout", "ko ", "tko", "clinches", "rally", "overtime", "pole")):
        return ("🔥 경기결과·활약", "#6ee7b7", "주요 경기 결과 및 핵심 선수 맹활약")
    if any(k in low for k in ("coach", "manager", "sack", "appoint", "ban", "suspend", "fine", "official", "confirm", "lineup", "start")):
        return ("📋 감독·공식발표", "#93c5fd", "구단·연맹 공식 발표 및 전술·기용 소식")
    if any(k in low for k in ("vote", "award", "mvp", "player of the year", "footballer of the year", "ranking", "preview", "playoff", "standings", "title", "champion")):
        return ("🏆 랭킹·프리뷰", "#fde68a", "주요 어워드·파워랭킹·타이틀 경쟁 브리핑")
    return ("⚡ 현지 주요속보", "#7dd3fc", "해외 주요 스포츠 매체 실시간 핵심 이슈")


def _format_overseas_kst(raw_time: str) -> str:
    s = (raw_time or "").strip()
    if not s:
        return datetime.now(KST).strftime("%m.%d %H:%M KST")
    if "KST" in s and re.search(r"\d{2}\.\d{2}", s):
        return s
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(KST).strftime("%m.%d %H:%M KST")
    except Exception:
        pass
    try:
        iso = s.replace("Z", "+00:00")
        dt2 = datetime.fromisoformat(iso)
        if dt2.tzinfo is None:
            dt2 = dt2.replace(tzinfo=timezone.utc)
        return dt2.astimezone(KST).strftime("%m.%d %H:%M KST")
    except Exception:
        return s[:22]


def enrich_overseas_news_item_ko(raw: Dict[str, Any]) -> Dict[str, Any]:
    title = _html_lib.unescape(str(raw.get("title") or "").strip())
    source = str(raw.get("source") or "해외속보").strip()
    link = str(raw.get("link") or raw.get("url") or "").strip()
    raw_sport = str(raw.get("sport_ko") or raw.get("sport") or "").strip()
    pub_raw = str(raw.get("pubDate") or raw.get("published") or raw.get("time_kst") or raw.get("summary_tag") or "").strip()

    sport_ko = _detect_overseas_sport_ko(title, source, raw_sport)
    sport_badge, sport_color = _SPORT_META_KO.get(sport_ko, ("⚽ 해외축구", "#38bdf8"))
    topic_badge, topic_color, topic_phrase = _detect_overseas_topic_ko(title)

    low_title = title.lower()
    matched_ko: List[str] = []
    for aliases, ko_label in _OVERSEAS_ENTITY_KO_MAP:
        if any(a in low_title for a in aliases):
            if ko_label not in matched_ko:
                matched_ko.append(ko_label)
        if len(matched_ko) >= 3:
            break

    if matched_ko:
        ko_summary = f"{' · '.join(matched_ko)} — {topic_phrase}"
    else:
        sport_plain = sport_badge.split(" ", 1)[-1]
        ko_summary = f"{sport_plain} · {topic_phrase}"

    return {
        "title": title,
        "link": link or "https://www.espn.com/",
        "url": link or "https://www.espn.com/",
        "source": source,
        "sport": sport_ko,
        "sport_ko": sport_ko,
        "sport_badge": sport_badge,
        "sport_color": sport_color,
        "topic_badge": topic_badge,
        "topic_color": topic_color,
        "ko_entities": matched_ko,
        "ko_summary": ko_summary,
        "pubDate": _format_overseas_kst(pub_raw),
        "published_kst": _format_overseas_kst(pub_raw),
    }


def _fetch_espn_sport_news_v22(sport_ko: str, source_label: str, api_url: str, rss_url: str = "") -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    try:
        req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0 SportIQ-News/2.2"})
        with urllib.request.urlopen(req, timeout=3.5) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
                for art in (data.get("articles") or [])[:8]:
                    hl = str(art.get("headline") or "").strip()
                    lk = str(((art.get("links") or {}).get("web") or {}).get("href") or "").strip()
                    pb = str(art.get("published") or "")
                    if hl:
                        items.append(
                            enrich_overseas_news_item_ko(
                                {"title": hl, "link": lk, "source": source_label, "sport_ko": sport_ko, "published": pb}
                            )
                        )
    except Exception:
        pass

    if len(items) < 3 and rss_url:
        try:
            req2 = urllib.request.Request(rss_url, headers={"User-Agent": "Mozilla/5.0 SportIQ-News/2.2"})
            with urllib.request.urlopen(req2, timeout=3.5) as resp2:
                if resp2.status == 200:
                    xml_txt = resp2.read().decode("utf-8", errors="replace")
                    root = ET.fromstring(xml_txt)
                    for it in root.findall(".//item")[:8]:
                        hl = (it.findtext("title") or "").strip()
                        lk = (it.findtext("link") or "").strip()
                        pb = (it.findtext("pubDate") or "").strip()
                        if hl:
                            items.append(
                                enrich_overseas_news_item_ko(
                                    {"title": hl, "link": lk, "source": source_label, "sport_ko": sport_ko, "pubDate": pb}
                                )
                            )
        except Exception:
            pass
    return items


_FALLBACK_EXTRA_SPORTS_V22: Tuple[Dict[str, str], ...] = (
    {
        "title": "NFL Power Rankings Week 12: Detroit Lions hold No. 1 spot after statement win, Bills surge past Chiefs",
        "link": "https://www.espn.com/nfl/",
        "source": "ESPN NFL",
        "sport_ko": "NFL",
    },
    {
        "title": "Chiefs QB Patrick Mahomes focuses on red-zone efficiency ahead of crucial AFC showdown",
        "link": "https://www.espn.com/nfl/",
        "source": "ESPN NFL",
        "sport_ko": "NFL",
    },
    {
        "title": "Ravens and Steelers injury report: Key defensive starters monitored ahead of AFC North clash",
        "link": "https://www.espn.com/nfl/",
        "source": "ESPN NFL",
        "sport_ko": "NFL",
    },
    {
        "title": "UFC Heavyweight Champion Jon Jones weighs next title defense options after Madison Square Garden victory",
        "link": "https://www.espn.com/mma/",
        "source": "ESPN MMA",
        "sport_ko": "UFC",
    },
    {
        "title": "Alex Pereira and Islam Makhachev headline UFC Pound-for-Pound rankings update for November",
        "link": "https://www.espn.com/mma/",
        "source": "ESPN MMA",
        "sport_ko": "UFC",
    },
    {
        "title": "Ilia Topuria targets lightweight superfight as UFC announces upcoming championship schedule",
        "link": "https://www.bbc.com/sport/mixed-martial-arts",
        "source": "BBC Sport MMA",
        "sport_ko": "UFC",
    },
    {
        "title": "Formula 1 Las Vegas Grand Prix preview: Max Verstappen can seal fourth consecutive world championship title",
        "link": "https://www.bbc.com/sport/formula1",
        "source": "BBC Formula 1",
        "sport_ko": "F1",
    },
    {
        "title": "McLaren and Ferrari battle for F1 Constructors' Championship lead under Las Vegas street circuit lights",
        "link": "https://www.espn.com/f1/",
        "source": "ESPN F1",
        "sport_ko": "F1",
    },
    {
        "title": "Lando Norris & Charles Leclerc setup analysis for cold-weather tarmac at Las Vegas GP qualifying",
        "link": "https://www.espn.com/f1/",
        "source": "ESPN F1",
        "sport_ko": "F1",
    },
)


def collect_enriched_overseas_news_v22(force: bool = False) -> List[Dict[str, Any]]:
    global _OVERSEAS_V22_CACHE
    now_m = time.monotonic()
    with _OVERSEAS_V22_LOCK:
        cached_ts, cached_items = _OVERSEAS_V22_CACHE
        if not force and cached_items and (now_m - cached_ts) < 600.0:
            return cached_items

    collected: List[Dict[str, Any]] = []
    seen_titles: Set[str] = set()

    def _add(item: Dict[str, Any]) -> None:
        key = re.sub(r"[^a-z0-9가-힣]", "", (item.get("title") or "").lower())[:64]
        if not key or key in seen_titles:
            return
        seen_titles.add(key)
        collected.append(item)

    # 1. Load existing cached BBC/ESPN rows from SQLite overseas_news_cache if available
    try:
        def _read_db_news() -> List[Dict[str, Any]]:
            with _smu_db_session(write=False) as conn:
                has_tbl = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='overseas_news_cache'"
                ).fetchone()
                if not has_tbl:
                    return []
                cols = {r[1] for r in conn.execute("PRAGMA table_info(overseas_news_cache)").fetchall()}
                sel_sport = "sport" if "sport" in cols else "'' AS sport"
                sel_pub = "published_at" if "published_at" in cols else ("summary_tag" if "summary_tag" in cols else "'' AS summary_tag")
                rows = conn.execute(
                    f"SELECT title, link, source, {sel_sport}, {sel_pub} FROM overseas_news_cache ORDER BY rowid DESC LIMIT 60"
                ).fetchall()
                return [dict(r) for r in rows]

        for r in run_with_db_retry(_read_db_news):
            _add(
                enrich_overseas_news_item_ko(
                    {
                        "title": r.get("title"),
                        "link": r.get("link"),
                        "source": r.get("source") or "BBC·ESPN",
                        "sport": r.get("sport") or "",
                        "pubDate": r.get("published_at") or r.get("summary_tag") or "",
                    }
                )
            )
    except Exception:
        pass

    # 2. Fetch live multi-sport feeds in parallel (Soccer, NBA, MLB, NHL, NFL, UFC, F1)
    feed_specs = [
        ("축구", "ESPN Soccer", "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/news?limit=8", "https://feeds.bbci.co.uk/sport/football/rss.xml"),
        ("농구", "ESPN NBA", "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/news?limit=8", "https://www.espn.com/espn/rss/nba/news"),
        ("야구", "ESPN MLB", "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/news?limit=8", "https://www.espn.com/espn/rss/mlb/news"),
        ("하키", "ESPN NHL", "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/news?limit=6", "https://www.espn.com/espn/rss/nhl/news"),
        ("NFL", "ESPN NFL", "https://site.api.espn.com/apis/site/v2/sports/football/nfl/news?limit=8", "https://www.espn.com/espn/rss/nfl/news"),
        ("UFC", "ESPN MMA", "https://site.api.espn.com/apis/site/v2/sports/mma/ufc/news?limit=8", "https://feeds.bbci.co.uk/sport/mixed-martial-arts/rss.xml"),
        ("F1", "BBC·ESPN F1", "https://site.api.espn.com/apis/site/v2/sports/racing/f1/news?limit=8", "https://feeds.bbci.co.uk/sport/formula1/rss.xml"),
    ]
    try:
        with ThreadPoolExecutor(max_workers=7) as pool:
            futs = [pool.submit(_fetch_espn_sport_news_v22, sp, src, au, ru) for sp, src, au, ru in feed_specs]
            for f in futs:
                try:
                    for it in f.result(timeout=4.2):
                        _add(it)
                except Exception:
                    continue
    except Exception:
        pass

    # 3. Guarantee every sport filter (especially NFL, UFC, F1) has rich items even in offline/sandbox mode
    counts_by_sp: Dict[str, int] = {}
    for it in collected:
        sk = it.get("sport_ko") or "축구"
        counts_by_sp[sk] = counts_by_sp.get(sk, 0) + 1

    now_kst_str = datetime.now(KST).strftime("%m.%d %H:%M KST")
    for fb in _FALLBACK_EXTRA_SPORTS_V22:
        if counts_by_sp.get(fb["sport_ko"], 0) < 3:
            enriched = enrich_overseas_news_item_ko({**fb, "pubDate": now_kst_str})
            _add(enriched)
            counts_by_sp[fb["sport_ko"]] = counts_by_sp.get(fb["sport_ko"], 0) + 1

    with _OVERSEAS_V22_LOCK:
        _OVERSEAS_V22_CACHE = (time.monotonic(), collected)
    return collected


@router.get("/api/lounge/overseas-news")
def get_enriched_overseas_news_api_v22(sport: str = "전체"):
    items = collect_enriched_overseas_news_v22(force=False)
    counts: Dict[str, int] = {"전체": len(items)}
    for sp_key in ("축구", "야구", "농구", "하키", "NFL", "UFC", "F1"):
        counts[sp_key] = sum(1 for x in items if x.get("sport_ko") == sp_key)

    sp_clean = (sport or "전체").strip()
    if sp_clean and sp_clean != "전체":
        filtered = [x for x in items if x.get("sport_ko") == sp_clean]
    else:
        filtered = items

    return {
        "ok": True,
        "sport": sp_clean,
        "counts": counts,
        "items": filtered[:48],
        "updated_at_kst": datetime.now(KST).strftime("%Y.%m.%d %H:%M KST"),
    }


router.include_router(upgrade_router)


# --- Stale-While-Revalidate (SWR) & Background Pre-Warmer for DataCenter Tabs ---
_DC_SWR_CACHE: Dict[int, tuple[float, list]] = {}
_DC_SWR_REFRESHING: set[int] = set()
_DC_SWR_LOCK = threading.Lock()
_DC_SWR_INSTALLED = False


def install_datacenter_swr_accelerator(dc_mod: Any) -> bool:
    global _DC_SWR_INSTALLED
    if getattr(dc_mod, "__swr_accelerated__", False):
        return True
    orig_collect = getattr(dc_mod, "_collect_all_matches", None)
    if not callable(orig_collect):
        return False

    def _fresh_ttl(offset: int) -> float:
        return 45.0 if offset == 0 else 600.0

    def _refresh_in_bg(offset: int) -> None:
        with _DC_SWR_LOCK:
            if offset in _DC_SWR_REFRESHING:
                return
            _DC_SWR_REFRESHING.add(offset)

        def _job() -> None:
            try:
                data = orig_collect(offset)
                if isinstance(data, list):
                    with _DC_SWR_LOCK:
                        _DC_SWR_CACHE[offset] = (time.monotonic(), data)
            except Exception:
                pass
            finally:
                with _DC_SWR_LOCK:
                    _DC_SWR_REFRESHING.discard(offset)

        threading.Thread(target=_job, daemon=True, name=f"dc-swr-{offset}").start()

    def _swr_collect(date_offset: int = 0):
        try:
            offset = int(date_offset)
        except (TypeError, ValueError):
            offset = 0
        now_m = time.monotonic()
        with _DC_SWR_LOCK:
            entry = _DC_SWR_CACHE.get(offset)
        if entry is not None:
            ts, cached_list = entry
            if (now_m - ts) <= _fresh_ttl(offset):
                return cached_list
            _refresh_in_bg(offset)
            return cached_list

        data = orig_collect(offset)
        if isinstance(data, list):
            with _DC_SWR_LOCK:
                _DC_SWR_CACHE[offset] = (time.monotonic(), data)
        return data

    dc_mod._collect_all_matches = _swr_collect
    dc_mod.__swr_accelerated__ = True
    _DC_SWR_INSTALLED = True
    return True


def _datacenter_prewarm_daemon() -> None:
    time.sleep(1.5)
    dc_mod = None
    for _ in range(20):
        try:
            from app import sportiq_datacenter as _dc
            if install_datacenter_swr_accelerator(_dc):
                dc_mod = _dc
                break
        except Exception:
            pass
        time.sleep(1.0)
    if dc_mod is None:
        return

    tick = 0
    while True:
        try:
            with _DC_SWR_LOCK:
                e0 = _DC_SWR_CACHE.get(0)
            if e0 is None:
                dc_mod._collect_all_matches(0)
            elif (time.monotonic() - e0[0]) >= 32.0:
                with _DC_SWR_LOCK:
                    _DC_SWR_CACHE[0] = (time.monotonic() - 46.0, e0[1])
                dc_mod._collect_all_matches(0)

            if tick % 15 == 0:
                with _DC_SWR_LOCK:
                    em1 = _DC_SWR_CACHE.get(-1)
                if em1 is None:
                    dc_mod._collect_all_matches(-1)
                else:
                    with _DC_SWR_LOCK:
                        _DC_SWR_CACHE[-1] = (time.monotonic() - 601.0, em1[1])
                    dc_mod._collect_all_matches(-1)
        except Exception:
            pass
        tick += 1
        time.sleep(35.0)


threading.Thread(target=_datacenter_prewarm_daemon, daemon=True, name="sportiq-dc-prewarm").start()


class PresencePingIn(BaseModel):
    session_id: str = "anon"
    role: Optional[str] = "GUEST"


class HashtagIn(BaseModel):
    tag: str


class PartnerInquiryIn(BaseModel):
    company: str
    contact_name: str
    contact_info: str
    tier: Optional[str] = "PREMIUM_SPONSOR"
    message: str


class CheerIn(BaseModel):
    session_id: str = "anon"
    author: str = "익명팬"
    role: Optional[str] = "GUEST"
    team: Optional[str] = "ALL"
    message: str


class ReportIn(BaseModel):
    session_id: Optional[str] = ""
    reason: Optional[str] = "신고 접수"


@router.get("/", response_class=HTMLResponse)
def serve_lounge_home():
    if os.path.exists(HTML_PATH):
        try:
            _ensure_upgrade_js_dedup_patched()
            with open(HTML_PATH, "r", encoding="utf-8") as f:
                raw_html = f.read()
            rendered = inject_upgrade_into_html(raw_html)
            rendered = re.sub(
                r'(/api/member/p0p1-upgrade\.js\?v=)(\d+)',
                lambda m: m.group(1) + str(int(m.group(2)) + 400),
                rendered,
            )
            return HTMLResponse(rendered, media_type="text/html; charset=utf-8")
        except Exception:
            return FileResponse(HTML_PATH, media_type="text/html; charset=utf-8")
    return HTMLResponse("<h1>SportIQ Lounge</h1><p>HTML file missing.</p>", status_code=200)


@router.get("/api/lounge/health")
def lounge_health():
    def _op() -> None:
        with _db_session(write=False) as conn:
            conn.execute("SELECT 1").fetchone()

    run_with_db_retry(_op)
    return {
        "status": "ok",
        "service": "sportiq-lounge",
        "storage": "sqlite-wal",
        "busy_timeout_ms": DEFAULT_BUSY_TIMEOUT_MS,
        "datacenter_swr_active": _DC_SWR_INSTALLED,
        "member_upgrade_v1": True,
        "match_sort_bar_dedup": _SORT_DEDUP_APPLIED,
        "ai_iq_hyper_search_v2": True,
        "member_ux_v22": True,
    }


def _presence_stats(conn: sqlite3.Connection) -> dict:
    now = time.time()
    today = datetime.now(KST).strftime("%Y-%m-%d")
    active = conn.execute(
        "SELECT COUNT(*) AS cnt FROM presence_sessions WHERE last_seen >= ?", (now - 120,)
    ).fetchone()["cnt"]
    today_cnt = conn.execute(
        "SELECT COUNT(*) AS cnt FROM presence_sessions WHERE visit_date = ?", (today,)
    ).fetchone()["cnt"]
    return {"active_now": max(active, 1), "today_visitors": max(today_cnt, 1)}


@router.post("/api/presence/ping")
def presence_ping(payload: PresencePingIn):
    now = time.time()
    today = datetime.now(KST).strftime("%Y-%m-%d")
    sid = (payload.session_id or "anon").strip()[:64]
    role = (payload.role or "GUEST").strip()[:24]

    def _op() -> dict:
        with _db_session(write=True) as conn:
            conn.execute(
                """
                INSERT INTO presence_sessions(session_id, role, visit_date, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    role = excluded.role,
                    visit_date = excluded.visit_date,
                    last_seen = excluded.last_seen
                """,
                (sid, role, today, now, now),
            )
            return _presence_stats(conn)

    stats = run_with_db_retry(_op)
    return {"ok": True, **stats}


@router.get("/api/presence")
def get_presence():
    def _op() -> dict:
        with _db_session(write=False) as conn:
            return _presence_stats(conn)

    return run_with_db_retry(_op)


@router.get("/api/hashtags/daily")
def get_daily_hashtags():
    def _op() -> list[dict]:
        with _db_session(write=False) as conn:
            rows = conn.execute(
                "SELECT tag, count, updated_at FROM daily_hashtags ORDER BY count DESC, updated_at DESC LIMIT 24"
            ).fetchall()
            return [dict(r) for r in rows]

    return {"hashtags": run_with_db_retry(_op)}


@router.post("/api/hashtags/register")
def register_hashtag(payload: HashtagIn):
    raw = (payload.tag or "").strip().replace(" ", "")
    if not raw:
        raise HTTPException(status_code=400, detail="해시태그를 입력해 주세요.")
    tag = raw if raw.startswith("#") else f"#{raw}"
    if len(tag) < 2 or len(tag) > 32:
        raise HTTPException(status_code=400, detail="해시태그는 1~30자 이내로 입력해 주세요.")
    now = time.time()

    def _op() -> list[dict]:
        with _db_session(write=True) as conn:
            conn.execute(
                """
                INSERT INTO daily_hashtags(tag, count, updated_at) VALUES (?, 1, ?)
                ON CONFLICT(tag) DO UPDATE SET count = count + 1, updated_at = excluded.updated_at
                """,
                (tag, now),
            )
            rows = conn.execute(
                "SELECT tag, count, updated_at FROM daily_hashtags ORDER BY count DESC, updated_at DESC LIMIT 24"
            ).fetchall()
            return [dict(r) for r in rows]

    hashtags = run_with_db_retry(_op)
    try:
        from app.sportiq_auto_analyst import _backup_tables_to_pg
        _backup_tables_to_pg("daily_hashtags")
    except Exception:
        pass
    return {"ok": True, "tag": tag, "hashtags": hashtags}


def _mask_contact(val: str) -> str:
    s = (val or "").strip()
    if not s:
        return ""
    if "@" in s:
        local, domain = s.split("@", 1)
        return f"{local[:2]}***@{domain}"
    if len(s) <= 6:
        return s[:2] + "*****"
    return f"{s[:3]}****{s[-3:]}"


@router.get("/api/partner-inquiries")
def list_partner_inquiries():
    def _op() -> list[dict]:
        with _db_session(write=False) as conn:
            rows = conn.execute(
                "SELECT id, company, contact_name, contact_info, tier, message, created_at FROM partner_inquiries ORDER BY id DESC LIMIT 50"
            ).fetchall()
            return [dict(r) for r in rows]

    rows = run_with_db_retry(_op)
    items = []
    for d in rows:
        d["contact_info"] = _mask_contact(d.get("contact_info", ""))
        items.append(d)
    return {"inquiries": items}


@router.post("/api/partner-inquiries")
def create_partner_inquiry(payload: PartnerInquiryIn):
    _c = payload.contact_info.strip()
    if not payload.company.strip() or not payload.contact_name.strip() or not payload.contact_info.strip():
        raise HTTPException(status_code=400, detail="업체명, 담당자명, 연락처를 모두 입력해 주세요.")
    if not (("@" in _c and "." in _c.split("@", 1)[-1]) or sum(ch.isdigit() for ch in _c) >= 8):
        raise HTTPException(status_code=400, detail="연락처는 유효한 이메일 또는 전화번호 형식으로 입력해 주세요.")
    now = time.time()

    def _op() -> int:
        with _db_session(write=True) as conn:
            cur = conn.execute(
                "INSERT INTO partner_inquiries(company, contact_name, contact_info, tier, message, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    payload.company.strip()[:80],
                    payload.contact_name.strip()[:60],
                    payload.contact_info.strip()[:100],
                    (payload.tier or "STANDARD").strip()[:40],
                    (payload.message or "").strip()[:1000],
                    now,
                ),
            )
            return int(cur.lastrowid or 0)

    new_id = run_with_db_retry(_op)
    try:
        from app.sportiq_auto_analyst import _backup_tables_to_pg
        _backup_tables_to_pg("partner_inquiries")
    except Exception:
        pass
    return {"ok": True, "id": new_id, "message": "파트너 입점 및 제휴 문의가 정상 접수되었습니다."}


@router.get("/api/cheers")
def list_cheers():
    def _op() -> list[dict]:
        with _db_session(write=False) as conn:
            rows = conn.execute(
                "SELECT id, author, role, team, message, created_at FROM cheers ORDER BY id DESC LIMIT 50"
            ).fetchall()
            return [dict(r) for r in rows]

    rows = run_with_db_retry(_op)
    result = []
    for r in rows:
        dt = _format_cheer_time(r.get("created_at"))
        ts = _to_epoch_ts(r.get("created_at"))
        result.append({**r, "created_at": ts if ts is not None else r.get("created_at"), "time_label": dt})
    return {"cheers": result}


@router.post("/api/cheers")
def post_cheer(payload: CheerIn, request: Request):
    msg = (payload.message or "").strip()
    if not msg:
        raise HTTPException(status_code=400, detail="응원 메시지를 입력해 주세요.")
    sid = (payload.session_id or (request.client.host if request.client else "anon")).strip()[:64]
    now = time.time()
    author = (payload.author or "익명팬").strip()[:30]
    role = (payload.role or "GUEST").strip()[:20]
    team = (payload.team or "ALL").strip()[:30]
    clean_msg = msg[:300]

    def _op() -> int:
        with _db_session(write=True) as conn:
            last = conn.execute(
                "SELECT created_at FROM cheers WHERE session_id = ? ORDER BY id DESC LIMIT 1", (sid,)
            ).fetchone()
            last_ts = _to_epoch_ts(last["created_at"]) if last else None
            if last_ts is not None and (now - last_ts) < 5.0:
                remain = max(1, int(round(5.0 - (now - last_ts))))
                raise HTTPException(status_code=429, detail=f"도배 방지를 위해 {remain}초 후 다시 전송할 수 있습니다.")
            cur = conn.execute(
                "INSERT INTO cheers(session_id, author, role, team, message, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (sid, author, role, team, clean_msg, now),
            )
            return int(cur.lastrowid or 0)

    new_id = run_with_db_retry(_op)
    dt = datetime.fromtimestamp(now, tz=KST).strftime("%H:%M:%S")
    return {
        "ok": True,
        "cheer": {
            "id": new_id,
            "author": author,
            "role": role,
            "team": team,
            "message": clean_msg,
            "created_at": now,
            "time_label": dt,
        },
    }


@router.post("/api/posts/{post_id}/report")
def report_post(post_id: str, payload: Optional[ReportIn] = None):
    now = time.time()
    sid = (payload.session_id if payload else "") or ""
    reason = (payload.reason if payload else "") or "커뮤니티 가이드라인 위반 신고"

    def _op() -> None:
        with _db_session(write=True) as conn:
            conn.execute(
                "INSERT INTO post_reports(post_id, session_id, reason, created_at) VALUES (?, ?, ?, ?)",
                (post_id[:64], sid[:64], reason[:200], now),
            )

    run_with_db_retry(_op)
    return {"ok": True, "post_id": post_id, "reported": True}


@router.get("/api/rankings/global-matches")
def get_global_matches():
    def _op() -> list[dict]:
        with _db_session(write=False) as conn:
            rows = conn.execute(
                "SELECT id, match_key, sport, league, league_color, matchup, schedule_venue, votes, buzz_score, rank_change FROM global_match_rankings ORDER BY votes DESC, buzz_score DESC, id ASC"
            ).fetchall()
            return [
                {
                    "rank": i + 1,
                    "id": r["id"],
                    "match_key": r["match_key"],
                    "sport": r["sport"],
                    "league": r["league"],
                    "league_color": r["league_color"],
                    "matchup": r["matchup"],
                    "schedule_venue": r["schedule_venue"],
                    "votes": r["votes"],
                    "buzz_score": r["buzz_score"],
                    "rank_change": r["rank_change"],
                }
                for i, r in enumerate(rows)
            ]

    return {"matches": run_with_db_retry(_op)}


@router.post("/api/rankings/global-matches/{match_id}/vote")
def vote_global_match(match_id: int):
    def _op() -> tuple[Optional[dict], list[dict]]:
        with _db_session(write=True) as conn:
            cur = conn.execute("UPDATE global_match_rankings SET votes = votes + 1 WHERE id = ?", (match_id,))
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="매치를 찾을 수 없습니다.")
            rows = conn.execute(
                "SELECT id, match_key, sport, league, league_color, matchup, schedule_venue, votes, buzz_score, rank_change FROM global_match_rankings ORDER BY votes DESC, buzz_score DESC, id ASC"
            ).fetchall()
            items = [
                {
                    "rank": i + 1,
                    "id": r["id"],
                    "match_key": r["match_key"],
                    "sport": r["sport"],
                    "league": r["league"],
                    "league_color": r["league_color"],
                    "matchup": r["matchup"],
                    "schedule_venue": r["schedule_venue"],
                    "votes": r["votes"],
                    "buzz_score": r["buzz_score"],
                    "rank_change": r["rank_change"],
                }
                for i, r in enumerate(rows)
            ]
            voted = next((m for m in items if m["id"] == match_id), None)
            return voted, items

    voted, items = run_with_db_retry(_op)
    try:
        from app.sportiq_auto_analyst import _backup_tables_to_pg
        _backup_tables_to_pg("global_match_rankings")
    except Exception:
        pass
    return {"ok": True, "match": voted, "matches": items}
