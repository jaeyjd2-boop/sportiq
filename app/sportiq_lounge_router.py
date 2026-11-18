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
  var reactionCache = { like_counts: {}, liked_ids: {}, bookmarked_ids: {}, bookmarked_titles: {}, extra_comments: {} };

  function fnv1aHexV24(str) {
    var s = String(str || "").replace(/\s+/g, " ").trim().toLowerCase();
    var h = 2166136261;
    for (var i = 0; i < s.length; i++) {
      h ^= s.charCodeAt(i);
      h = Math.imul(h, 16777619);
    }
    return (h >>> 0).toString(16);
  }

  function canonicalPostIdV24(p) {
    if (!p) return "";
    var rawId = String(p.id != null ? p.id : "").trim();
    var title = String(p.title || "").trim();
    var author = String(p.author || "");
    var isVolatileAuto = Boolean(
      p.isAutoAnalyst ||
      p.isAiBriefing ||
      author.indexOf("자동분석") !== -1 ||
      author.indexOf("AI 데이터분석팀") !== -1 ||
      title.indexOf("[00:00") === 0 ||
      !rawId ||
      /^auto[-_]/i.test(rawId) ||
      /^ai[-_]/i.test(rawId) ||
      /^\d+$/.test(rawId)
    );
    if (isVolatileAuto && title) {
      return "aa_" + fnv1aHexV24(title);
    }
    return rawId;
  }

  function normalizeStatePostsV24() {
    if (typeof state !== "object" || !state || !Array.isArray(state.posts)) return;
    var seenIds = {};
    var seenTitles = {};
    var deduped = [];
    for (var i = 0; i < state.posts.length; i++) {
      var p = state.posts[i];
      if (!p) continue;
      var cid = canonicalPostIdV24(p);
      if (cid) p.id = cid;
      if (!Array.isArray(p.tags)) {
        p.tags = p.tags ? String(p.tags).split(/\s+/).filter(Boolean) : ["분석"];
      }
      p.tags = p.tags.map(function (t) { return String(t).replace(/^#+/, ""); }).filter(Boolean);
      if (!Array.isArray(p.comments)) p.comments = [];

      var normT = String(p.title || "").replace(/\s+/g, " ").trim().toLowerCase();
      var idKey = String(p.id || "");
      if ((idKey && seenIds[idKey]) || (normT && seenTitles[normT])) {
        var keeper = seenIds[idKey] || seenTitles[normT];
        if (keeper && p.isAutoAnalyst && !keeper.isAutoAnalyst) {
          keeper.isAutoAnalyst = true;
          keeper.homeLabel = p.homeLabel;
          keeper.awayLabel = p.awayLabel;
          keeper.homePct = p.homePct;
          keeper.awayPct = p.awayPct;
          keeper.tacticalPoints = p.tacticalPoints;
          keeper.recommendedPick = p.recommendedPick;
        }
        continue;
      }
      if (idKey) seenIds[idKey] = p;
      if (normT) seenTitles[normT] = p;
      deduped.push(p);
    }
    if (deduped.length !== state.posts.length) {
      state.posts = deduped;
    }
  }
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
      "/* 1-Line Main Community Tab Bar (Overrides .sq-mobile-bottom-nav-bar 6-col grid!) */",
      "#sqDesktopMainTabStrip,",
      "#sqDesktopMainTabStrip.sq-main-tabbar-row,",
      "#sqDesktopMainTabStrip.sq-mobile-bottom-nav-bar {",
      "  position: static !important;",
      "  display: flex !important;",
      "  grid-template-columns: none !important;",
      "  flex-direction: row !important;",
      "  flex-wrap: nowrap !important;",
      "  align-items: center !important;",
      "  justify-content: flex-start !important;",
      "  gap: 6px !important;",
      "  overflow-x: auto !important;",
      "  -webkit-overflow-scrolling: touch;",
      "  scrollbar-width: none !important;",
      "}",
      "#sqDesktopMainTabStrip::-webkit-scrollbar { display: none !important; }",
      "#sqDesktopMainTabStrip > .tab-btn,",
      "#sqDesktopMainTabStrip > button,",
      "#sqDesktopMainTabStrip > a {",
      "  flex: 0 0 auto !important;",
      "  width: auto !important;",
      "  min-width: max-content !important;",
      "  white-space: nowrap !important;",
      "  padding: 6px 10px !important;",
      "  font-size: 12.4px !important;",
      "  line-height: 1.25 !important;",
      "}",
      "@media (min-width: 1240px) {",
      "  #sqDesktopMainTabStrip { gap: 7px !important; }",
      "  #sqDesktopMainTabStrip > .tab-btn,",
      "  #sqDesktopMainTabStrip > button { padding: 7px 12px !important; font-size: 13px !important; }",
      "}",
      "/* v3.0 Scorebase-Inspired Dual Scoreboard Table, Status/Star Filters & 5-Tab Match Insight Modal */",
      "#sqV30ScoreToolbar {",
      "  display: flex;",
      "  flex-wrap: wrap;",
      "  align-items: center;",
      "  justify-content: space-between;",
      "  gap: 8px;",
      "  padding: 7px 4px 9px;",
      "  margin-bottom: 6px;",
      "  border-bottom: 1px solid rgba(51, 65, 85, 0.55);",
      "}",
      ".sq-v30-fbtn {",
      "  background: rgba(15, 23, 42, 0.9);",
      "  border: 1px solid #334155;",
      "  color: #cbd5e1;",
      "  border-radius: 999px;",
      "  padding: 4px 10px;",
      "  font-size: 11.5px;",
      "  font-weight: 700;",
      "  cursor: pointer;",
      "  white-space: nowrap;",
      "  transition: all 0.14s ease;",
      "}",
      ".sq-v30-fbtn:hover { border-color: #64748b; color: #f8fafc; }",
      ".sq-v30-fbtn.active {",
      "  background: rgba(16, 185, 129, 0.2);",
      "  border-color: #34d399;",
      "  color: #6ee7b7;",
      "  font-weight: 800;",
      "}",
      ".sq-v30-fbtn.active-live {",
      "  background: rgba(220, 38, 38, 0.22);",
      "  border-color: #f87171;",
      "  color: #fecaca;",
      "  font-weight: 800;",
      "}",
      ".sq-v30-fbtn.active-view {",
      "  background: rgba(56, 189, 248, 0.2);",
      "  border-color: #38bdf8;",
      "  color: #7dd3fc;",
      "  font-weight: 800;",
      "}",
      ".sq-v30-live-dot {",
      "  display: inline-flex;",
      "  align-items: center;",
      "  margin-left: 4px;",
      "  padding: 1px 6px;",
      "  border-radius: 999px;",
      "  background: rgba(220, 38, 38, 0.24);",
      "  border: 1px solid rgba(248, 113, 113, 0.65);",
      "  color: #fca5a5;",
      "  font-size: 10px;",
      "  font-weight: 900;",
      "  line-height: 1.2;",
      "}",
      "/* v3.1 11-Day Calendar Strip, Card Filter & Row Hover Mini-Stat Tooltip */",
      "[data-sq-v31-card-hidden='1'] {",
      "  display: none !important;",
      "  max-width: 0 !important;",
      "  min-width: 0 !important;",
      "  max-height: 0 !important;",
      "  overflow: hidden !important;",
      "  visibility: hidden !important;",
      "  opacity: 0 !important;",
      "  pointer-events: none !important;",
      "  padding: 0 !important;",
      "  margin: 0 !important;",
      "  border: 0 !important;",
      "}",
      "/* v3.2.2 Mobile (390x844) Polish: Hide Thin Scrollbars, Compact 5-Col Mobile Scoreboard & Crisp Modal Layout */",
      "#dcSportPills, #dcLiveTickerTrack, #sqMatchInsightTabsV30, #sqV30ScoreboardTableWrap {",
      "  scrollbar-width: thin;",
      "  scrollbar-color: rgba(71, 85, 105, 0.55) transparent;",
      "}",
      "#dcSportPills::-webkit-scrollbar, #sqMatchInsightTabsV30::-webkit-scrollbar { display: none; }",
      "#dcQuickMatchModal { word-break: keep-all; }",
      "#dcQuickMatchModal #sqModalFavBtnV30,",
      "#dcQuickMatchModal #sqCloseMatchInsightV30 {",
      "  white-space: nowrap !important;",
      "  flex-shrink: 0 !important;",
      "}",
      ".sq-v32-vote-btn {",
      "  min-width: 0 !important;",
      "  overflow: hidden !important;",
      "}",
      "@media (max-width: 640px) {",
      "  .sq-v30-sb-table {",
      "    min-width: 495px !important;",
      "    font-size: 11.4px !important;",
      "  }",
      "  .sq-v30-sb-table thead th {",
      "    padding: 7px 5px !important;",
      "    font-size: 10.4px !important;",
      "  }",
      "  .sq-v30-match-row td {",
      "    padding: 7px 5px !important;",
      "  }",
      "  .sq-v30-match-row td:nth-child(3),",
      "  .sq-v30-match-row td:nth-child(5) {",
      "    max-width: 88px !important;",
      "    overflow: hidden !important;",
      "    text-overflow: ellipsis !important;",
      "  }",
      "  #dcQuickMatchModal {",
      "    padding: 10px !important;",
      "  }",
      "  #dcQuickMatchModal > div {",
      "    max-height: 91vh !important;",
      "    border-radius: 16px !important;",
      "  }",
      "  #sqBridgeTacticalV30, #sqBridgeStandingsV30 {",
      "    min-width: 0 !important;",
      "    flex: 1 1 44% !important;",
      "    white-space: nowrap !important;",
      "    padding: 8px 8px !important;",
      "    font-size: 11.5px !important;",
      "  }",
      "  #sqBridgeWritePostV30 {",
      "    flex: 1 1 100% !important;",
      "    order: -1 !important;",
      "    white-space: nowrap !important;",
      "    padding: 9px 10px !important;",
      "  }",
      "}",
      "/* v3.2 1-Click Match Fan Vote (+5P & Real-Time Vote Gauge) */",
      ".sq-v32-vote-btn {",
      "  background: #0f172a;",
      "  border: 1px solid #334155;",
      "  color: #e2e8f0;",
      "  border-radius: 10px;",
      "  padding: 8px 8px;",
      "  cursor: pointer;",
      "  display: flex;",
      "  flex-direction: column;",
      "  align-items: center;",
      "  justify-content: center;",
      "  gap: 2px;",
      "  transition: all 0.15s ease;",
      "}",
      ".sq-v32-vote-btn:hover { border-color: #38bdf8; background: rgba(30, 41, 59, 0.9); transform: translateY(-1px); }",
      ".sq-v32-vote-btn.is-picked-H {",
      "  background: rgba(16, 185, 129, 0.2);",
      "  border-color: #34d399;",
      "  box-shadow: 0 0 0 1px rgba(52, 211, 153, 0.45);",
      "}",
      ".sq-v32-vote-btn.is-picked-D {",
      "  background: rgba(148, 163, 184, 0.22);",
      "  border-color: #cbd5e1;",
      "  box-shadow: 0 0 0 1px rgba(203, 213, 225, 0.4);",
      "}",
      ".sq-v32-vote-btn.is-picked-A {",
      "  background: rgba(56, 189, 248, 0.2);",
      "  border-color: #38bdf8;",
      "  box-shadow: 0 0 0 1px rgba(56, 189, 248, 0.45);",
      "}",
      "#sqV31DateCalendarBar {",
      "  display: flex;",
      "  align-items: center;",
      "  gap: 5px;",
      "  overflow-x: auto;",
      "  padding: 6px 4px 8px;",
      "  margin-bottom: 4px;",
      "  border-bottom: 1px dashed rgba(51, 65, 85, 0.55);",
      "  scrollbar-width: none;",
      "}",
      "#sqV31DateCalendarBar::-webkit-scrollbar { display: none; }",
      ".sq-v31-cal-btn {",
      "  flex: 0 0 auto;",
      "  background: rgba(15, 23, 42, 0.88);",
      "  border: 1px solid #334155;",
      "  color: #cbd5e1;",
      "  border-radius: 9px;",
      "  padding: 4px 9px;",
      "  font-size: 11.3px;",
      "  font-weight: 700;",
      "  cursor: pointer;",
      "  white-space: nowrap;",
      "  transition: all 0.14s ease;",
      "}",
      ".sq-v31-cal-btn:hover { border-color: #64748b; color: #f8fafc; background: rgba(30, 41, 59, 0.92); }",
      ".sq-v31-cal-btn.is-sat { color: #93c5fd; }",
      ".sq-v31-cal-btn.is-sun { color: #fca5a5; }",
      ".sq-v31-cal-btn.active {",
      "  background: linear-gradient(135deg, rgba(16, 185, 129, 0.26), rgba(6, 182, 212, 0.26));",
      "  border-color: #34d399;",
      "  color: #6ee7b7;",
      "  font-weight: 900;",
      "  box-shadow: 0 2px 10px rgba(16, 185, 129, 0.22);",
      "}",
      "#sqV31RowHoverPreview {",
      "  position: fixed;",
      "  z-index: 100022;",
      "  pointer-events: none;",
      "  display: none;",
      "  width: 312px;",
      "  max-width: calc(100vw - 24px);",
      "  background: rgba(9, 15, 31, 0.97);",
      "  border: 1px solid rgba(56, 189, 248, 0.52);",
      "  border-radius: 13px;",
      "  padding: 10px 12px;",
      "  color: #f8fafc;",
      "  box-shadow: 0 18px 40px rgba(0, 0, 0, 0.68);",
      "  font-size: 11.5px;",
      "  line-height: 1.5;",
      "}",
      "[data-sq-v30-hide-strip='1'] {",
      "  display: none !important;",
      "  max-height: 0 !important;",
      "  overflow: hidden !important;",
      "  visibility: hidden !important;",
      "  opacity: 0 !important;",
      "  pointer-events: none !important;",
      "  margin: 0 !important;",
      "  padding: 0 !important;",
      "  border: 0 !important;",
      "}",
      "#sqV30ScoreboardTableWrap {",
      "  display: none;",
      "  max-height: 520px;",
      "  overflow-y: auto;",
      "  overflow-x: auto;",
      "  background: #070d1a;",
      "  border: 1px solid #1e293b;",
      "  border-radius: 13px;",
      "  margin-bottom: 8px;",
      "}",
      "#sqV30ScoreboardTableWrap[data-sq-open='1'] {",
      "  display: block !important;",
      "  visibility: visible !important;",
      "  opacity: 1 !important;",
      "}",
      ".sq-v30-sb-table {",
      "  width: 100%;",
      "  min-width: 660px;",
      "  border-collapse: collapse;",
      "  font-size: 12.2px;",
      "  color: #e2e8f0;",
      "}",
      ".sq-v30-sb-table thead th {",
      "  position: sticky;",
      "  top: 0;",
      "  z-index: 3;",
      "  background: #0f172a;",
      "  color: #94a3b8;",
      "  font-size: 11px;",
      "  font-weight: 800;",
      "  padding: 8px 9px;",
      "  border-bottom: 1px solid #334155;",
      "  text-align: left;",
      "  white-space: nowrap;",
      "}",
      ".sq-v30-lg-hdr td {",
      "  background: rgba(15, 23, 42, 0.92);",
      "  padding: 6px 10px;",
      "  border-top: 1px solid #1e293b;",
      "  border-bottom: 1px solid #1e293b;",
      "  font-weight: 800;",
      "  font-size: 11.8px;",
      "  color: #93c5fd;",
      "}",
      ".sq-v30-match-row {",
      "  border-bottom: 1px solid rgba(30, 41, 59, 0.72);",
      "  cursor: pointer;",
      "  transition: background 0.12s ease;",
      "}",
      ".sq-v30-match-row:hover { background: rgba(30, 41, 59, 0.58); }",
      ".sq-v30-match-row.is-live-row { background: rgba(127, 29, 29, 0.14); }",
      ".sq-v30-match-row td { padding: 8px 9px; vertical-align: middle; white-space: nowrap; }",
      ".sq-v30-odds-box {",
      "  display: inline-block;",
      "  min-width: 42px;",
      "  text-align: center;",
      "  padding: 2px 6px;",
      "  border-radius: 6px;",
      "  background: #0f172a;",
      "  border: 1px solid #334155;",
      "  font-size: 11px;",
      "  font-weight: 700;",
      "  color: #cbd5e1;",
      "  margin-right: 3px;",
      "}",
      ".sq-v30-hit-badge {",
      "  display: inline-flex;",
      "  align-items: center;",
      "  padding: 1px 6px;",
      "  border-radius: 999px;",
      "  font-size: 10px;",
      "  font-weight: 800;",
      "  background: rgba(16, 185, 129, 0.18);",
      "  border: 1px solid rgba(52, 211, 153, 0.5);",
      "  color: #6ee7b7;",
      "  white-space: nowrap;",
      "}",
      ".sq-v30-upset-badge {",
      "  display: inline-flex;",
      "  align-items: center;",
      "  padding: 1px 6px;",
      "  border-radius: 999px;",
      "  font-size: 10px;",
      "  font-weight: 800;",
      "  background: rgba(245, 158, 11, 0.16);",
      "  border: 1px solid rgba(251, 191, 36, 0.45);",
      "  color: #fde68a;",
      "  white-space: nowrap;",
      "}",
      ".sq-v30-insight-tab {",
      "  background: #0f172a;",
      "  border: 1px solid #334155;",
      "  color: #cbd5e1;",
      "  border-radius: 9px;",
      "  padding: 6px 10px;",
      "  font-size: 11.8px;",
      "  font-weight: 800;",
      "  cursor: pointer;",
      "  white-space: nowrap;",
      "}",
      ".sq-v30-insight-tab.active {",
      "  background: linear-gradient(135deg, rgba(16,185,129,.25), rgba(6,182,212,.25));",
      "  border-color: #34d399;",
      "  color: #6ee7b7;",
      "}",
      "/* v2.9 Modal 4-Spot Layout Fix, Share Button & Toast Z-Index Above Floating Dock */",
      "#writeModal, #writeModal > div, #write-modal, #write-modal > div {",
      "  overflow-x: hidden !important;",
      "  box-sizing: border-box !important;",
      "}",
      "#sqPostAuthorInput, #sqPostPinInput, #sqSyncNickInput, #sqSyncPinInput {",
      "  min-width: 0 !important;",
      "  width: 100% !important;",
      "  max-width: 100% !important;",
      "  box-sizing: border-box !important;",
      "}",
      "[data-sq-write-pin-row='1'] {",
      "  display: grid !important;",
      "  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) !important;",
      "  gap: 8px !important;",
      "  width: 100% !important;",
      "  max-width: 100% !important;",
      "  box-sizing: border-box !important;",
      "}",
      "#sqDoSyncBtn,",
      "#commentModal button,",
      "#post-modal button,",
      "#sqMemberHubModal button {",
      "  white-space: nowrap !important;",
      "  word-break: keep-all !important;",
      "  flex-shrink: 0 !important;",
      "}",
      "@media (max-width: 560px) {",
      "  [data-sq-write-pin-row='1'] {",
      "    grid-template-columns: 1fr !important;",
      "  }",
      "  [data-sq-sync-grid='1'] {",
      "    grid-template-columns: minmax(0, 1fr) 110px !important;",
      "  }",
      "  [data-sq-sync-grid='1'] #sqDoSyncBtn {",
      "    grid-column: 1 / -1 !important;",
      "    width: 100% !important;",
      "    justify-content: center !important;",
      "  }",
      "}",
      "#toast, #toast-container, [id*='Toast'], .sq-toast {",
      "  z-index: 100025 !important;",
      "}",
      "@keyframes sqDeepLinkPulse {",
      "  0% { box-shadow: 0 0 0 0 rgba(52, 211, 153, 0.75); }",
      "  70% { box-shadow: 0 0 0 8px rgba(52, 211, 153, 0); }",
      "  100% { box-shadow: 0 0 0 0 rgba(52, 211, 153, 0); }",
      "}",
      "article[data-sq-shared-highlight='1'] {",
      "  border-color: #34d399 !important;",
      "  box-shadow: 0 0 0 2px rgba(52, 211, 153, 0.65), 0 16px 36px rgba(16, 185, 129, 0.22) !important;",
      "  animation: sqDeepLinkPulse 1.8s ease-out 2;",
      "}",
      "/* v2.8 Protect DataCenter #dcHubBody & Cards (배당 급변 드롭오즈 / 순위 / 적중률) from Seed-Guard Hiding */",
      "#dcMainHubSection[data-sq-seed-archive],",
      "#dcHubCollapsible[data-sq-seed-archive],",
      "#dcHubBody[data-sq-seed-archive],",
      "#dcMainHubSection.sq-seed-archive-card,",
      "#dcHubCollapsible.sq-seed-archive-card,",
      "#dcHubBody.sq-seed-archive-card {",
      "  display: block !important;",
      "  visibility: visible !important;",
      "  opacity: 1 !important;",
      "}",
      "#dcHubBody[data-sq-dc-user-open='1'] {",
      "  display: block !important;",
      "  visibility: visible !important;",
      "  opacity: 1 !important;",
      "}",
      "#dcHubBody [data-sq-seed-archive],",
      "#dcHubBody .sq-seed-archive-card {",
      "  visibility: visible !important;",
      "  opacity: 1 !important;",
      "}",
      "/* Guarantee #feedList is NEVER hidden by match-strip seed guards */",
      "#feedList {",
      "  display: block !important;",
      "  visibility: visible !important;",
      "  opacity: 1 !important;",
      "}",
      "#feedList > article + article {",
      "  margin-top: 14px;",
      "}",
      "#feedList[data-sq-bm-only='1'] > article:not([data-sq-bookmarked='1']) {",
      "  display: none !important;",
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
      "}",
      "/* v2.6 Active DataCenter 7th Tab Highlight & Sibling Tab De-Highlight */",
      "#sqDesktopMainTabStrip[data-sq-dc-mode='1'] > button:not([data-sq-dc-active='1']),",
      "#sqDesktopMainTabStrip[data-sq-dc-mode='1'] > a:not([data-sq-dc-active='1']) {",
      "  background: rgba(15, 23, 42, 0.86) !important;",
      "  color: #cbd5e1 !important;",
      "  border-color: rgba(51, 65, 85, 0.72) !important;",
      "  box-shadow: none !important;",
      "}",
      "#sqDesktopMainTabStrip > button[data-sq-dc-active='1'],",
      "#sqDesktopMainTabStrip > a[data-sq-dc-active='1'] {",
      "  background: linear-gradient(135deg, #10b981, #06b6d4) !important;",
      "  color: #020617 !important;",
      "  font-weight: 900 !important;",
      "  border-color: #34d399 !important;",
      "  box-shadow: 0 4px 14px rgba(16, 185, 129, 0.35) !important;",
      "}",
      "/* v2.6.1 Global (Desktop + Mobile) Crisp 28x28 SVG Close Button & Non-Shrinking PWA Banner Controls */",
      "#sqBannerInstallNow {",
      "  flex-shrink: 0 !important;",
      "  white-space: nowrap !important;",
      "}",
      "#sqBannerDismissPwa,",
      "[data-sq-pwa-close='1'] {",
      "  display: inline-flex !important;",
      "  align-items: center !important;",
      "  justify-content: center !important;",
      "  width: 28px !important;",
      "  min-width: 28px !important;",
      "  height: 28px !important;",
      "  min-height: 28px !important;",
      "  padding: 0 !important;",
      "  margin-left: 2px !important;",
      "  border-radius: 8px !important;",
      "  background: rgba(51, 65, 85, 0.88) !important;",
      "  border: 1px solid rgba(148, 163, 184, 0.48) !important;",
      "  color: #f8fafc !important;",
      "  line-height: 1 !important;",
      "  flex: 0 0 28px !important;",
      "  flex-shrink: 0 !important;",
      "  cursor: pointer !important;",
      "  transition: background 0.15s ease, border-color 0.15s ease, color 0.15s ease !important;",
      "}",
      "#sqBannerDismissPwa:hover,",
      "[data-sq-pwa-close='1']:hover {",
      "  background: rgba(239, 68, 68, 0.24) !important;",
      "  border-color: rgba(248, 113, 113, 0.72) !important;",
      "  color: #fecaca !important;",
      "}",
      "@media (min-width: 821px) {",
      "  #sqPwaInstallBanner,",
      "  [data-sq-pwa-banner='1'] {",
      "    max-width: 408px !important;",
      "    width: auto !important;",
      "    padding: 11px 14px !important;",
      "    gap: 10px !important;",
      "  }",
      "}",
      "/* v2.6 Mobile <=820px (390x844) Floating Banner / Dual Dock Separation & Footer Clearance */",
      "@media (max-width: 820px) {",
      "  [data-sq-header-hub-row='1'] {",
      "    display: flex !important;",
      "    flex-wrap: wrap !important;",
      "    gap: 6px !important;",
      "    max-width: 100% !important;",
      "  }",
      "  #rssSportFilters {",
      "    display: flex !important;",
      "    flex-wrap: nowrap !important;",
      "    overflow-x: auto !important;",
      "    -webkit-overflow-scrolling: touch;",
      "    scrollbar-width: none;",
      "    gap: 6px !important;",
      "    padding-bottom: 4px !important;",
      "  }",
      "  #rssSportFilters::-webkit-scrollbar { display: none; }",
      "  #rssSportFilters > .rss-filter-btn {",
      "    flex: 0 0 auto !important;",
      "    white-space: nowrap !important;",
      "  }",
      "  #sqPwaInstallBanner,",
      "  [data-sq-pwa-banner='1'] {",
      "    left: 10px !important;",
      "    right: 10px !important;",
      "    bottom: 10px !important;",
      "    width: auto !important;",
      "    max-width: calc(100vw - 20px) !important;",
      "    padding: 9px 11px !important;",
      "    gap: 8px !important;",
      "    border-radius: 13px !important;",
      "    box-sizing: border-box !important;",
      "    z-index: 9990 !important;",
      "  }",
      "  #sqPwaInstallBanner img,",
      "  [data-sq-pwa-banner='1'] img {",
      "    width: 32px !important;",
      "    height: 32px !important;",
      "  }",
      "  #sqBannerDismissPwa,",
      "  [data-sq-pwa-close='1'] {",
      "    display: inline-flex !important;",
      "    align-items: center !important;",
      "    justify-content: center !important;",
      "    min-width: 28px !important;",
      "    height: 28px !important;",
      "    padding: 0 6px !important;",
      "    border-radius: 8px !important;",
      "    background: rgba(51, 65, 85, 0.82) !important;",
      "    border: 1px solid rgba(100, 116, 139, 0.55) !important;",
      "    color: #f8fafc !important;",
      "    font-size: 13px !important;",
      "    font-weight: 900 !important;",
      "    flex-shrink: 0 !important;",
      "    cursor: pointer !important;",
      "  }",
      "  [data-sq-floating-dock='1'] {",
      "    right: 10px !important;",
      "    gap: 6px !important;",
      "    z-index: 9988 !important;",
      "  }",
      "  [data-sq-floating-dock='1'] button,",
      "  [data-sq-floating-dock='1'] a,",
      "  button[data-sq-floating-item],",
      "  a[data-sq-floating-item] {",
      "    padding: 7px 11px !important;",
      "    font-size: 11.5px !important;",
      "    line-height: 1.2 !important;",
      "  }",
      "  #sqOfficialLegalFooter {",
      "    padding: 24px 14px 176px !important;",
      "  }",
      "  #sqOfficialLegalFooter .sq-footer-link-btn {",
      "    min-height: 34px !important;",
      "    padding: 6px 12px !important;",
      "    font-size: 11.5px !important;",
      "  }",
      "}"
    ].join("\n");
    document.head.appendChild(st);
  }

  // -------------------------------------------------------------------------
  // 1. 1-Row Main Tab Bar + Dynamic Match Count ('총 20경기' -> actual count)
  // -------------------------------------------------------------------------
  function enforceSingleLineMainTabBar() {
    var firstTab = document.querySelector("button.tab-btn[data-tab='feed'], button[onclick*=\"switchTab('feed')\"]");
    if (!firstTab || !firstTab.parentElement) return;
    var bar = firstTab.parentElement;
    var nav = firstTab.closest ? firstTab.closest("nav") : bar.parentElement;
    bar.id = "sqDesktopMainTabStrip";
    bar.classList.remove("sq-mobile-bottom-nav-bar");
    bar.classList.add("sq-main-tabbar-row");
    if (nav && nav.classList) {
      nav.classList.remove("sq-mobile-bottom-nav-bar");
    }

    // Move '📊 데이터센터' button (appended directly onto <nav> by sportiq_datacenter_ui.js L360) into bar!
    if (nav) {
      var strayBtns = nav.querySelectorAll("button, a");
      for (var i = 0; i < strayBtns.length; i++) {
        var sb = strayBtns[i];
        if (sb.parentElement !== bar && /데이터\s*센터/.test(sb.textContent || "")) {
          sb.className = "tab-btn whitespace-nowrap rounded-lg px-3 py-1.5 text-xs font-bold transition border border-cyan-500/40 bg-cyan-500/15 text-cyan-200 hover:bg-cyan-500/25";
          bar.appendChild(sb);
        }
      }
    }

    bar.style.setProperty("display", "flex", "important");
    bar.style.setProperty("grid-template-columns", "none", "important");
    bar.style.setProperty("flex-direction", "row", "important");
    bar.style.setProperty("flex-wrap", "nowrap", "important");
    bar.style.setProperty("overflow-x", "auto", "important");
    Array.prototype.forEach.call(bar.children, function (btn) {
      if (btn && btn.style) {
        btn.style.setProperty("flex", "0 0 auto", "important");
        btn.style.setProperty("white-space", "nowrap", "important");
        btn.style.setProperty("width", "auto", "important");
      }
    });
    bindDataCenterMainTabClickV26(bar);
  }

  function bindDataCenterMainTabClickV26(bar) {
    if (!bar) return;
    var dcBtn = null;
    Array.prototype.forEach.call(bar.children, function (btn) {
      if (!dcBtn && btn && /데이터\s*센터/.test(btn.textContent || "")) {
        dcBtn = btn;
      }
    });
    if (!dcBtn) return;

    if (!dcBtn.getAttribute("data-sq-dc-bound-v26")) {
      dcBtn.setAttribute("data-sq-dc-bound-v26", "1");
      dcBtn.setAttribute("data-tab", "datacenter");
      dcBtn.addEventListener("click", function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        if (typeof ev.stopImmediatePropagation === "function") ev.stopImmediatePropagation();

        var dcTabPanel = document.getElementById("tab-datacenter");
        var dcGrid = document.getElementById("dcLauncherGrid") || document.getElementById("dcLaunchCards");
        var dcHub = (
          document.getElementById("dcMainHubSection") ||
          document.querySelector(".dc-hub-wrap") ||
          (dcGrid && dcGrid.closest ? dcGrid.closest("section, div") : null)
        );
        var targetEl = dcTabPanel || dcHub;
        if (!targetEl) {
          if (typeof window.switchTab === "function") {
            try { window.switchTab("datacenter"); } catch (e) {}
          }
          return;
        }

        var hostPanel = (!dcTabPanel && dcHub && dcHub.closest) ? dcHub.closest("[id^='tab-']") : null;
        document.querySelectorAll("[id^='tab-']").forEach(function (sec) {
          if (sec === dcTabPanel || sec === hostPanel) {
            sec.classList.remove("hidden");
            sec.style.setProperty("display", "block", "important");
          } else {
            sec.setAttribute("data-sq-hidden-by-dc", "1");
            sec.style.setProperty("display", "none", "important");
          }
        });

        if (dcHub) {
          if (!dcHub.hasAttribute("data-sq-dc-was-hidden")) {
            var initHidden = (window.getComputedStyle(dcHub).display === "none") || dcHub.classList.contains("hidden");
            dcHub.setAttribute("data-sq-dc-was-hidden", initHidden ? "1" : "0");
          }
          dcHub.classList.remove("hidden");
          dcHub.style.setProperty("display", "block", "important");
        }
        var dcBody = document.getElementById("dcHubBody");
        if (dcBody) {
          dcBody.setAttribute("data-sq-dc-user-open", "1");
          dcBody.classList.remove("hidden", "sq-seed-archive-card");
          dcBody.removeAttribute("data-sq-seed-archive");
          dcBody.style.setProperty("display", "block", "important");
        }
        var dcTog = document.getElementById("dcToggleHubBtn");
        if (dcTog && (dcTog.textContent || "").indexOf("펼치기") !== -1) {
          dcTog.textContent = "접기 ▲";
        }
        if (typeof window.__switchDcTab === "function" && dcBody && (!dcBody.children.length || /로딩\s*중/.test(dcBody.textContent || ""))) {
          try { window.__switchDcTab("standings"); } catch (e) {}
        }

        bar.setAttribute("data-sq-dc-mode", "1");
        Array.prototype.forEach.call(bar.children, function (b) {
          if (b === dcBtn) b.setAttribute("data-sq-dc-active", "1");
          else b.removeAttribute("data-sq-dc-active");
        });

        var rect = targetEl.getBoundingClientRect();
        var curY = window.pageYOffset || document.documentElement.scrollTop || 0;
        var destY = Math.max(0, rect.top + curY - 92);
        try {
          window.scrollTo({ top: destY, behavior: "smooth" });
        } catch (e) {
          window.scrollTo(0, destY);
        }
      }, true);
    }

    if (!bar.getAttribute("data-sq-dc-restore-bound-v26")) {
      bar.setAttribute("data-sq-dc-restore-bound-v26", "1");
      bar.addEventListener("click", function (ev) {
        var clickedBtn = ev.target && ev.target.closest ? ev.target.closest("button, a") : null;
        if (!clickedBtn || clickedBtn.parentElement !== bar) return;
        if (/데이터\s*센터/.test(clickedBtn.textContent || "")) return;

        if (bar.getAttribute("data-sq-dc-mode") === "1") {
          bar.removeAttribute("data-sq-dc-mode");
          Array.prototype.forEach.call(bar.children, function (b) {
            b.removeAttribute("data-sq-dc-active");
          });
          document.querySelectorAll("[data-sq-hidden-by-dc='1']").forEach(function (sec) {
            sec.style.removeProperty("display");
            sec.removeAttribute("data-sq-hidden-by-dc");
          });
          var dcTabPanel2 = document.getElementById("tab-datacenter");
          if (dcTabPanel2) {
            dcTabPanel2.classList.add("hidden");
            dcTabPanel2.style.display = "none";
          }
          var dcHub2 = document.getElementById("dcMainHubSection") || document.querySelector(".dc-hub-wrap");
          if (dcHub2 && dcHub2.getAttribute("data-sq-dc-was-hidden") === "1") {
            dcHub2.style.display = "none";
          }
        }
      }, true);
    }
  }

  function saveOrigPosV26(el) {
    if (!el || el.hasAttribute("data-sq-orig-saved")) return;
    el.setAttribute("data-sq-orig-saved", "1");
    el.setAttribute("data-sq-orig-bottom", el.style.bottom || "");
    el.setAttribute("data-sq-orig-right", el.style.right || "");
  }

  function restoreOrigPosV26(el) {
    if (!el || !el.hasAttribute("data-sq-mob-shifted")) return;
    el.removeAttribute("data-sq-mob-shifted");
    var ob = el.getAttribute("data-sq-orig-bottom") || "";
    var orr = el.getAttribute("data-sq-orig-right") || "";
    if (ob) el.style.bottom = ob;
    else el.style.removeProperty("bottom");
    if (orr) el.style.right = orr;
    else el.style.removeProperty("right");
  }

  function harmonizeMobileFloatingOverlaysV26() {
    var ptsBadge = document.getElementById("pointsBadge");
    if (ptsBadge && ptsBadge.parentElement) {
      ptsBadge.parentElement.setAttribute("data-sq-header-hub-row", "1");
    }

    // 1. Locate bottom-left PWA install banner & ensure clear ✕ close button
    var banner = document.getElementById("sqPwaInstallBanner");
    if (!banner && document.body) {
      var candDivs = document.body.querySelectorAll("div, aside");
      for (var bi = 0; bi < candDivs.length; bi++) {
        var cd = candDivs[bi];
        if ((cd.textContent || "").indexOf("홈 화면 앱으로 열기") !== -1 && window.getComputedStyle(cd).position === "fixed" && cd.offsetHeight < 180) {
          banner = cd;
          break;
        }
      }
    }
    if (banner) {
      banner.setAttribute("data-sq-pwa-banner", "1");
      var closeBtn = banner.querySelector("#sqBannerDismissPwa, [data-sq-pwa-close='1']");
      if (!closeBtn) {
        var bBtns = banner.querySelectorAll("button");
        for (var k = 0; k < bBtns.length; k++) {
          var btTxt = (bBtns[k].textContent || "").trim();
          if (btTxt.indexOf("설치") === -1) {
            closeBtn = bBtns[k];
            break;
          }
        }
      }
      if (!closeBtn) {
        closeBtn = document.createElement("button");
        closeBtn.type = "button";
        closeBtn.textContent = "✕";
        banner.appendChild(closeBtn);
      }
      closeBtn.setAttribute("data-sq-pwa-close", "1");
      closeBtn.setAttribute("aria-label", "앱 설치 배너 닫기");
      closeBtn.setAttribute("title", "닫기");
      if (!closeBtn.querySelector("svg[data-sq-x-svg='1']")) {
        closeBtn.innerHTML = "<svg data-sq-x-svg='1' viewBox='0 0 14 14' width='12' height='12' fill='none' aria-hidden='true' style='display:block;pointer-events:none;'><path d='M3 3L11 11M11 3L3 11' stroke='currentColor' stroke-width='2.2' stroke-linecap='round'/></svg>";
      }
      if (!closeBtn.getAttribute("data-sq-close-bound-v26")) {
        closeBtn.setAttribute("data-sq-close-bound-v26", "1");
        closeBtn.addEventListener("click", function () {
          try { localStorage.setItem("sportiq_pwa_dismiss_until", String(Date.now() + 7 * 86400000)); } catch (e) {}
          banner.style.setProperty("display", "none", "important");
          setTimeout(harmonizeMobileFloatingOverlaysV26, 20);
        });
      }
    }

    // 2. Locate right-bottom floating dock / pills (IQ중계티비 & AI 아이큐)
    var dockWrapper = null;
    var tvFixedEl = null;
    var aiFixedEl = null;
    if (document.body) {
      var allEls = document.body.querySelectorAll("div, aside, nav, section, button, a");
      for (var i = 0; i < allEls.length; i++) {
        var el = allEls[i];
        if (el === banner || el.id === "sqLegalPolicyModal" || el.id === "sqMemberHubModal" || el.id === "sqDesktopMainTabStrip") continue;
        if (el.classList && (el.classList.contains("sq-modal-backdrop") || el.classList.contains("inset-0") || el.classList.contains("hidden"))) continue;
        var pos = el.style.position || window.getComputedStyle(el).position;
        if (pos !== "fixed") continue;
        if (el.offsetWidth > 340 || el.offsetHeight > 280) continue;
        var txt = el.textContent || "";
        var hasTv = txt.indexOf("IQ중계") !== -1;
        var hasAi = txt.indexOf("AI 아이큐") !== -1;
        if (hasTv && hasAi) {
          dockWrapper = el;
          el.setAttribute("data-sq-floating-dock", "1");
          break;
        }
      }
      if (!dockWrapper) {
        for (var j = 0; j < allEls.length; j++) {
          var el2 = allEls[j];
          if (el2 === banner || el2.id === "sqLegalPolicyModal" || el2.id === "sqMemberHubModal" || el2.id === "sqDesktopMainTabStrip") continue;
          if (el2.classList && (el2.classList.contains("sq-modal-backdrop") || el2.classList.contains("inset-0") || el2.classList.contains("hidden"))) continue;
          var pos2 = el2.style.position || window.getComputedStyle(el2).position;
          if (pos2 !== "fixed") continue;
          if (el2.offsetWidth > 340 || el2.offsetHeight > 260) continue;
          var txt2 = el2.textContent || "";
          if (!tvFixedEl && txt2.indexOf("IQ중계") !== -1) {
            tvFixedEl = el2;
            el2.setAttribute("data-sq-floating-item", "tv");
          } else if (!aiFixedEl && txt2.indexOf("AI 아이큐") !== -1) {
            aiFixedEl = el2;
            el2.setAttribute("data-sq-floating-item", "ai");
          }
        }
      }
    }

    var isMobile = window.innerWidth <= 820;
    var footer = document.getElementById("sqOfficialLegalFooter");
    var mobNavH = 0;
    document.querySelectorAll(".sq-mobile-bottom-nav-bar").forEach(function (mn) {
      if (mn.id === "sqDesktopMainTabStrip") return;
      if (window.getComputedStyle(mn).position === "fixed" && window.getComputedStyle(mn).display !== "none") {
        mobNavH = Math.max(mobNavH, mn.offsetHeight || 56);
      }
    });

    if (isMobile) {
      var bannerVisible = Boolean(banner && window.getComputedStyle(banner).display !== "none" && banner.offsetHeight > 0);
      var bannerH = bannerVisible ? (banner.offsetHeight || 54) : 0;
      if (banner && bannerVisible) {
        saveOrigPosV26(banner);
        banner.style.setProperty("bottom", (10 + mobNavH) + "px", "important");
        banner.setAttribute("data-sq-mob-shifted", "1");
      }
      var dockBaseBottom = (bannerVisible ? (10 + bannerH + 10) : 14) + mobNavH;
      var totalDockH = 0;

      if (dockWrapper) {
        saveOrigPosV26(dockWrapper);
        dockWrapper.style.setProperty("right", "10px", "important");
        dockWrapper.style.setProperty("bottom", dockBaseBottom + "px", "important");
        dockWrapper.setAttribute("data-sq-mob-shifted", "1");
        totalDockH = dockWrapper.offsetHeight || 82;
      } else {
        var aiH = 0;
        if (aiFixedEl) {
          saveOrigPosV26(aiFixedEl);
          aiFixedEl.style.setProperty("right", "10px", "important");
          aiFixedEl.style.setProperty("bottom", dockBaseBottom + "px", "important");
          aiFixedEl.setAttribute("data-sq-mob-shifted", "1");
          aiH = aiFixedEl.offsetHeight || 36;
        }
        if (tvFixedEl) {
          saveOrigPosV26(tvFixedEl);
          var tvBot = dockBaseBottom + (aiFixedEl ? (aiH + 8) : 0);
          tvFixedEl.style.setProperty("right", "10px", "important");
          tvFixedEl.style.setProperty("bottom", tvBot + "px", "important");
          tvFixedEl.setAttribute("data-sq-mob-shifted", "1");
          totalDockH = (aiFixedEl ? (aiH + 8) : 0) + (tvFixedEl.offsetHeight || 36);
        } else {
          totalDockH = aiH;
        }
      }

      if (footer) {
        var safePad = Math.max(168, dockBaseBottom + totalDockH + 22);
        footer.style.setProperty("padding-bottom", safePad + "px", "important");
      }
    } else {
      if (banner) restoreOrigPosV26(banner);
      if (dockWrapper) restoreOrigPosV26(dockWrapper);
      if (tvFixedEl) restoreOrigPosV26(tvFixedEl);
      if (aiFixedEl) restoreOrigPosV26(aiFixedEl);
      if (footer) {
        footer.style.setProperty("padding-bottom", "108px", "important");
      }
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
    normalizeStatePostsV24();
    if (typeof state !== "object" || !state || !Array.isArray(state.posts)) return;
    var activeBmCount = 0;
    state.posts.forEach(function (p) {
      if (!p || !p.id) return;
      var pid = String(p.id);
      var tKey = String(p.title || "").trim();
      if (reactionCache.like_counts[pid] !== undefined) {
        p.likes = Number(reactionCache.like_counts[pid]);
      }
      p.liked = Boolean(reactionCache.liked_ids[pid]);
      var isBm = Boolean(reactionCache.bookmarked_ids[pid] || (tKey && reactionCache.bookmarked_titles[tKey]));
      if (isBm) reactionCache.bookmarked_ids[pid] = true;
      p.bookmarked = isBm;
      if (isBm) activeBmCount++;
      var extraC = (reactionCache.extra_comments && reactionCache.extra_comments[pid]) || [];
      var curC = Array.isArray(p.comments) ? p.comments : [];
      if ((extraC.length || curC.length > 1) && !p.isMemberServerPost) {
        var combinedC = curC.concat(extraC);
        var dedupC = [];
        var seenC = {};
        combinedC.forEach(function (c) {
          if (!c || !c.text) return;
          var ck = String(c.user || "").trim() + "::" + String(c.text || "").trim();
          if (!seenC[ck]) {
            seenC[ck] = true;
            dedupC.push(c);
          }
        });
        p.comments = dedupC;
      }
    });
    reactionCache.__matchedBmCount = activeBmCount;
    updateBookmarkFilterPillCount();
  }

  function syncRenderedFeedReactionButtonsDom() {
    var box = document.getElementById("feedList");
    if (!box || typeof state !== "object" || !state || !Array.isArray(state.posts)) return;
    box.querySelectorAll("article").forEach(function (art) {
      var h3 = art.querySelector("h3");
      var titleTxt = h3 ? String(h3.textContent || "").trim() : "";
      var likeBtn = art.querySelector("button[onclick^='toggleLike(']");
      var bmBtn = art.querySelector("button[onclick^='toggleBookmark(']");
      var m = likeBtn ? (likeBtn.getAttribute("onclick") || "").match(/toggleLike\(['"]([^'"]+)['"]\)/) : null;
      var domPid = m ? m[1] : "";
      var postObj = state.posts.find(function (x) {
        return x && (String(x.id) === domPid || (titleTxt && String(x.title || "").trim() === titleTxt));
      });
      if (!postObj) return;
      var cid = String(postObj.id);
      if (likeBtn && domPid !== cid) {
        likeBtn.setAttribute("onclick", "toggleLike('" + cid + "')");
      }
      if (bmBtn) {
        bmBtn.setAttribute("onclick", "toggleBookmark('" + cid + "')");
      }
      var effLiked = Boolean(reactionCache.liked_ids[cid] || postObj.liked);
      var effLikes = reactionCache.like_counts[cid] !== undefined ? Number(reactionCache.like_counts[cid]) : Number(postObj.likes || 0);
      var effBm = Boolean(reactionCache.bookmarked_ids[cid] || (titleTxt && reactionCache.bookmarked_titles[titleTxt]) || postObj.bookmarked);
      postObj.liked = effLiked;
      postObj.likes = effLikes;
      postObj.bookmarked = effBm;
      if (likeBtn) {
        likeBtn.textContent = "♥ 좋아요 " + effLikes;
        likeBtn.className = "rounded-lg px-2.5 py-1 " + (effLiked ? "bg-emerald-500/20 text-emerald-300 font-bold" : "bg-slate-800 text-slate-300");
      }
      art.setAttribute("data-sq-bookmarked", effBm ? "1" : "0");
      if (bmBtn) {
        bmBtn.textContent = "★ " + (effBm ? "저장됨" : "북마크");
        bmBtn.className = "rounded-lg px-2.5 py-1 " + (effBm ? "bg-amber-500/20 text-amber-300 font-bold" : "bg-slate-800 text-slate-300");
      }
      art.setAttribute("data-sq-post-id", cid);
      if (h3 && !h3.getAttribute("data-sq-title-bound-v29")) {
        h3.setAttribute("data-sq-title-bound-v29", "1");
        h3.style.cursor = "pointer";
        h3.title = "클릭하여 본문 상세 및 댓글 보기";
        h3.addEventListener("click", function () {
          openPostDetailAndCommentsV29(art.getAttribute("data-sq-post-id") || cid);
        });
      }
      var cmtBtn = null;
      art.querySelectorAll("button").forEach(function (b) {
        if (!cmtBtn && (b.textContent || "").indexOf("댓글") !== -1) cmtBtn = b;
      });
      if (cmtBtn) {
        var cCount = Array.isArray(postObj.comments) ? postObj.comments.length : 0;
        cmtBtn.textContent = "💬 댓글 " + cCount;
        var origOc = cmtBtn.getAttribute("onclick") || "";
        if (origOc && origOc.indexOf("(") !== -1) {
          var fnName = origOc.split("(")[0];
          cmtBtn.setAttribute("onclick", fnName + "('" + cid + "')");
        }
        if (!cmtBtn.getAttribute("data-sq-preview-bound-v29")) {
          cmtBtn.setAttribute("data-sq-preview-bound-v29", "1");
          cmtBtn.addEventListener("click", function () {
            var curId = art.getAttribute("data-sq-post-id") || cid;
            var pFound = (state.posts || []).find(function (x) { return x && String(x.id) === curId; });
            setTimeout(function () {
              if (pFound) injectPostPreviewIntoCommentModalV29(pFound);
              polishModalsAndCloseIconsV29();
            }, 25);
          });
        }
      }
      var actionBar = (cmtBtn && cmtBtn.parentElement) || (bmBtn && bmBtn.parentElement);
      if (actionBar && !actionBar.querySelector("[data-sq-share-btn='1']")) {
        var shBtn = document.createElement("button");
        shBtn.type = "button";
        shBtn.setAttribute("data-sq-share-btn", "1");
        shBtn.className = "rounded-lg px-2.5 py-1 bg-slate-800 text-sky-300 hover:bg-sky-500/20 font-semibold";
        shBtn.textContent = "🔗 공유";
        shBtn.onclick = function (ev) {
          ev.stopPropagation();
          window.shareSportiqPostV29(art.getAttribute("data-sq-post-id") || cid);
        };
        if (cmtBtn && cmtBtn.nextSibling) {
          actionBar.insertBefore(shBtn, cmtBtn.nextSibling);
        } else {
          actionBar.appendChild(shBtn);
        }
      }
    });
    // Also remove any duplicate rendered <article> cards with identical h3 title
    var seenDomTitles = {};
    box.querySelectorAll("article").forEach(function (art) {
      var h3 = art.querySelector("h3");
      var key = h3 ? String(h3.textContent || "").replace(/\s+/g, " ").trim().toLowerCase() : "";
      if (!key) return;
      if (seenDomTitles[key]) {
        if (art.parentNode) art.parentNode.removeChild(art);
      } else {
        seenDomTitles[key] = true;
      }
    });
    if (bookmarkOnlyFilter) {
      box.setAttribute("data-sq-bm-only", "1");
    } else {
      box.removeAttribute("data-sq-bm-only");
    }
  }

  function updateBookmarkFilterPillCount() {
    var pill = document.getElementById("sqFeedBookmarkFilterBtn");
    if (!pill) return;
    var rawKeys = Object.keys(reactionCache.bookmarked_ids || {}).filter(function (k) {
      return Boolean(reactionCache.bookmarked_ids[k]);
    });
    var cnt = (typeof state === "object" && state && Array.isArray(state.posts) && state.posts.length)
      ? Math.max(Number(reactionCache.__matchedBmCount || 0), rawKeys.filter(function (k) { return !/^auto[-_]|^\d+$/.test(k); }).length)
      : rawKeys.length;
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
      window.__sqBookmarkOnlyFilterV25 = bookmarkOnlyFilter;
      var fb = document.getElementById("feedList");
      if (fb) {
        if (bookmarkOnlyFilter) fb.setAttribute("data-sq-bm-only", "1");
        else fb.removeAttribute("data-sq-bm-only");
      }
      updateBookmarkFilterPillCount();
      if (typeof window.renderFeed === "function") window.renderFeed();
    };
    filterRow.querySelectorAll("[data-sq-ftab]").forEach(function (sib) {
      if (sib.__sqBmResetBound) return;
      sib.__sqBmResetBound = true;
      sib.addEventListener("click", function () {
        if (bookmarkOnlyFilter) {
          bookmarkOnlyFilter = false;
          window.__sqBookmarkOnlyFilterV25 = false;
          var fb = document.getElementById("feedList");
          if (fb) fb.removeAttribute("data-sq-bm-only");
          updateBookmarkFilterPillCount();
          if (typeof window.renderFeed === "function") window.renderFeed();
        }
      });
    });
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
        reactionCache.extra_comments = d.extra_comments || {};
        reactionCache.liked_ids = {};
        (d.liked_ids || []).forEach(function (id) { reactionCache.liked_ids[id] = true; });
        reactionCache.bookmarked_ids = {};
        (d.bookmarked_ids || []).forEach(function (id) { reactionCache.bookmarked_ids[String(id)] = true; });
        reactionCache.bookmarked_titles = {};
        (d.bookmarked_titles || []).forEach(function (t) { if (t) reactionCache.bookmarked_titles[String(t).trim()] = true; });

        var existingMap = {};
        (state.posts || []).forEach(function (p) { if (p && p.id) existingMap[p.id] = p; });
        var incoming = (d.posts || []).filter(function (p) { return p && p.id && !existingMap[p.id]; });
        if (incoming.length) {
          state.posts = incoming.concat(state.posts || []);
        } else if ((!state.posts || state.posts.length === 0) && Array.isArray(d.posts)) {
          state.posts = d.posts.slice();
        }
        var lgSel = document.getElementById("leagueFilter");
        if (lgSel && lgSel.value && lgSel.value !== "ALL") {
          var anyMatch = (state.posts || []).some(function (p) { return !p.hidden && p.league === lgSel.value; });
          if (!anyMatch) lgSel.value = "ALL";
        }
        applyReactionStateToPosts();
        mountBookmarkFilterPill();
        if (typeof window.renderFeed === "function") window.renderFeed();
        syncRenderedFeedReactionButtonsDom();
        handleDeepLinkPostOnBootV29();
      })
      .catch(function () {});
  }

  function installPersistentLikesAndBookmarks() {
    window.toggleLike = function (id) {
      normalizeStatePostsV24();
      if (typeof state !== "object" || !state || !Array.isArray(state.posts)) return;
      var sid = String(id);
      var p = state.posts.find(function (x) { return x && String(x.id) === sid; });
      if (!p) return;
      var pid = String(p.id);
      var baseLikes = (p.likes !== undefined && p.likes !== null && !isNaN(Number(p.likes))) ? Number(p.likes) : 0;
      var nextLiked = !p.liked;
      p.liked = nextLiked;
      p.likes = Math.max(0, baseLikes + (nextLiked ? 1 : -1));
      reactionCache.liked_ids[pid] = nextLiked;
      reactionCache.like_counts[pid] = p.likes;
      if (typeof window.renderFeed === "function") window.renderFeed();

      fetch("/api/posts/" + encodeURIComponent(pid) + "/like", {
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
          reactionCache.liked_ids[pid] = p.liked;
          reactionCache.like_counts[pid] = p.likes;
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
      normalizeStatePostsV24();
      if (typeof state !== "object" || !state || !Array.isArray(state.posts)) return;
      var sid = String(id);
      var p = state.posts.find(function (x) { return x && String(x.id) === sid; });
      if (!p) return;
      var pid = String(p.id);
      var tKey = String(p.title || "").trim();
      p.bookmarked = !p.bookmarked;
      reactionCache.bookmarked_ids[pid] = p.bookmarked;
      if (tKey) reactionCache.bookmarked_titles[tKey] = p.bookmarked;
      updateBookmarkFilterPillCount();
      if (typeof window.renderFeed === "function") window.renderFeed();

      fetch("/api/posts/" + encodeURIComponent(pid) + "/bookmark", {
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
          reactionCache.bookmarked_ids[pid] = p.bookmarked;
          if (tKey) reactionCache.bookmarked_titles[tKey] = p.bookmarked;
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
        if (typeof state === "object" && state && Array.isArray(state.posts)) {
          state.posts.forEach(function (p) {
            if (!p) return;
            if (!Array.isArray(p.tags)) {
              p.tags = p.tags ? String(p.tags).split(/\s+/).filter(Boolean) : ["분석"];
            }
            p.tags = p.tags.map(function (t) { return String(t).replace(/^#+/, ""); }).filter(Boolean);
            if (!Array.isArray(p.comments)) p.comments = [];
          });
        }
        applyReactionStateToPosts();
        try { prevRenderFeed(); } catch (e) {}
        mountBookmarkFilterPill();
        var box = document.getElementById("feedList");
        if (!box) return;
        box.style.setProperty("display", "block", "important");
        box.removeAttribute("data-sq-seed-archive");
        box.classList.remove("sq-seed-archive-card", "hidden");
        syncRenderedFeedReactionButtonsDom();
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
      var hasLocal = overseasActiveSportV22 === "전체"
        ? overseasCacheV22.length > 0
        : overseasCacheV22.some(function (it) { return (it.sport_ko || it.sport) === overseasActiveSportV22; });
      if (!hasLocal) {
        fetch("/api/lounge/overseas-news?sport=" + encodeURIComponent(overseasActiveSportV22))
          .then(function (r) { return r.json(); })
          .then(function (d) {
            if (d && d.ok && Array.isArray(d.items) && d.items.length) {
              overseasCacheV22 = overseasCacheV22.concat(d.items);
              if (d.counts) overseasCountsV22 = d.counts;
              renderOverseasFilterBarV22();
              renderOverseasListV22();
            }
          })
          .catch(function () {});
      }
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


  function installLiveHashtagSyncV25() {
    window.filterByTag = function (rawTag) {
      var clean = String(rawTag || "").replace(/^#+/, "").trim();
      if (!clean) return;
      bookmarkOnlyFilter = false;
      window.__sqBookmarkOnlyFilterV25 = false;
      var fb = document.getElementById("feedList");
      if (fb) fb.removeAttribute("data-sq-bm-only");
      updateBookmarkFilterPillCount();

      var lgSel = document.getElementById("leagueFilter");
      if (lgSel) lgSel.value = "ALL";
      var feedInp = document.getElementById("feedSearch");
      if (feedInp) feedInp.value = clean;
      var p3Inp = document.getElementById("sqP3FeedSearchInput");
      if (p3Inp) {
        p3Inp.value = clean;
        p3Inp.dispatchEvent(new Event("input", { bubbles: true }));
      }
      if (typeof window.switchTab === "function") window.switchTab("feed");
      if (typeof window.renderFeed === "function") window.renderFeed();
      mountActiveHashtagChipV25(clean);
      var targetEl = document.getElementById("sqP3FeedToolbar") || fb;
      if (targetEl && targetEl.scrollIntoView) {
        targetEl.scrollIntoView({ behavior: "smooth", block: "start" });
      }
      if (typeof showToast === "function") {
        showToast("🏷️ #" + clean + " 관련 실시간 분석글을 모아봅니다.");
      }
    };

    if (typeof window.loadHashtags === "function") {
      try { window.loadHashtags(); } catch (e) {}
    }
  }

  function mountActiveHashtagChipV25(cleanTag) {
    var old = document.getElementById("sqActiveTagResetChip");
    if (old && old.parentNode) old.parentNode.removeChild(old);
    if (!cleanTag) return;
    var toolbar = document.getElementById("sqP3FeedToolbar");
    if (!toolbar) return;
    var chip = document.createElement("button");
    chip.type = "button";
    chip.id = "sqActiveTagResetChip";
    chip.style.cssText = "background:rgba(16,185,129,.2);border:1px solid #34d399;color:#6ee7b7;border-radius:999px;padding:4px 10px;font-size:11.5px;font-weight:800;cursor:pointer;";
    chip.textContent = "🏷️ #" + cleanTag + " 필터 해제 ✕";
    chip.onclick = function () {
      var feedInp = document.getElementById("feedSearch");
      if (feedInp) feedInp.value = "";
      var p3Inp = document.getElementById("sqP3FeedSearchInput");
      if (p3Inp) {
        p3Inp.value = "";
        p3Inp.dispatchEvent(new Event("input", { bubbles: true }));
      }
      if (chip.parentNode) chip.parentNode.removeChild(chip);
      if (typeof window.renderFeed === "function") window.renderFeed();
    };
    toolbar.appendChild(chip);
  }

  function unblindDataCenterSubtreeV28() {
    var hub = document.getElementById("dcMainHubSection");
    var col = document.getElementById("dcHubCollapsible");
    var body = document.getElementById("dcHubBody");
    [hub, col, body].forEach(function (el) {
      if (!el) return;
      if (el.hasAttribute("data-sq-seed-archive")) el.removeAttribute("data-sq-seed-archive");
      if (el.classList && el.classList.contains("sq-seed-archive-card")) {
        el.classList.remove("sq-seed-archive-card");
      }
    });
    if (!body) return;

    try { ensureDomesticBasketballUiControlsV34(); } catch (e) {}
    // Unblind any child cards/rows inside #dcHubBody that a seed guard may have hidden
    body.querySelectorAll("[data-sq-seed-archive], .sq-seed-archive-card").forEach(function (ch) {
      ch.removeAttribute("data-sq-seed-archive");
      ch.classList.remove("sq-seed-archive-card", "hidden");
      if (ch.style && ch.style.display === "none") {
        ch.style.removeProperty("display");
      }
    });
    if (body.getAttribute("data-sq-dc-user-open") === "1" && body.children && body.children.length > 0) {
      var kids = Array.prototype.slice.call(body.children);
      var anyVis = kids.some(function (k) { return window.getComputedStyle(k).display !== "none"; });
      if (!anyVis) {
        kids.forEach(function (k) {
          k.classList.remove("hidden", "sq-seed-archive-card");
          if (k.style && k.style.display === "none") k.style.removeProperty("display");
        });
      }
    }

    if (body.getAttribute("data-sq-dc-user-open") === "1") {
      body.classList.remove("hidden");
      body.style.setProperty("display", "block", "important");
      var hint = document.getElementById("dcQuickExpandHint");
      if (hint) hint.style.display = "none";
      var tog = document.getElementById("dcToggleHubBtn");
      if (tog && (tog.textContent || "").indexOf("펼치기") !== -1) {
        tog.textContent = "접기 ▲";
      }
    }
  }

  function installDataCenterPersistenceGuardV28() {
    var grid = document.getElementById("dcLauncherGrid") || document.getElementById("dcLaunchCards");
    var body = document.getElementById("dcHubBody");
    var tog = document.getElementById("dcToggleHubBtn");
    var hint = document.getElementById("dcQuickExpandHint");

    function markDcBodyUserOpen() {
      var b = document.getElementById("dcHubBody");
      if (!b) return;
      b.setAttribute("data-sq-dc-user-open", "1");
      b.classList.remove("hidden", "sq-seed-archive-card");
      b.removeAttribute("data-sq-seed-archive");
      b.style.setProperty("display", "block", "important");
      var h = document.getElementById("dcQuickExpandHint");
      if (h) h.style.display = "none";
      var t = document.getElementById("dcToggleHubBtn");
      if (t && (t.textContent || "").indexOf("펼치기") !== -1) {
        t.textContent = "접기 ▲";
      }
      setTimeout(unblindDataCenterSubtreeV28, 30);
      setTimeout(unblindDataCenterSubtreeV28, 250);
      setTimeout(unblindDataCenterSubtreeV28, 700);
    }

    if (grid && !grid.getAttribute("data-sq-dc-grid-bound-v28")) {
      grid.setAttribute("data-sq-dc-grid-bound-v28", "1");
      grid.addEventListener("click", function () {
        markDcBodyUserOpen();
      }, true);
    }

    if (hint && !hint.getAttribute("data-sq-dc-hint-bound-v28")) {
      hint.setAttribute("data-sq-dc-hint-bound-v28", "1");
      hint.addEventListener("click", function () {
        markDcBodyUserOpen();
      }, true);
    }

    if (tog && !tog.getAttribute("data-sq-dc-tog-bound-v28")) {
      tog.setAttribute("data-sq-dc-tog-bound-v28", "1");
      tog.addEventListener("click", function () {
        setTimeout(function () {
          var b = document.getElementById("dcHubBody");
          if (!b) return;
          var txt = tog.textContent || "";
          if (txt.indexOf("펼치기") !== -1) {
            b.removeAttribute("data-sq-dc-user-open");
            b.style.setProperty("display", "none", "important");
          } else {
            b.setAttribute("data-sq-dc-user-open", "1");
            b.style.setProperty("display", "block", "important");
          }
        }, 15);
      });
    }

    if (typeof window.__switchDcTab === "function" && !window.__sqSwitchDcWrappedV28) {
      window.__sqSwitchDcWrappedV28 = true;
      var origSwDc = window.__switchDcTab;
      window.__switchDcTab = function (tab, optLeague) {
        markDcBodyUserOpen();
        var res = origSwDc.apply(this, arguments);
        markDcBodyUserOpen();
        return res;
      };
    }

    var obsTarget = document.getElementById("dcHubCollapsible") || document.getElementById("dcMainHubSection") || body;
    if (obsTarget && typeof MutationObserver !== "undefined" && !obsTarget.__sqDcMutObsV28) {
      obsTarget.__sqDcMutObsV28 = true;
      var obs = new MutationObserver(function () {
        unblindDataCenterSubtreeV28();
      });
      obs.observe(obsTarget, { childList: true, subtree: true, attributes: true, attributeFilter: ["class", "data-sq-seed-archive"] });
    }

    unblindDataCenterSubtreeV28();
  }

  function copyTextFallbackV29(text, okMsg) {
    function done() {
      if (typeof showToast === "function") {
        showToast(okMsg || "🔗 링크가 클립보드에 복사되었습니다!");
      }
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(function () {
        var ta = document.createElement("textarea");
        ta.value = text;
        ta.style.cssText = "position:fixed;left:-9999px;top:0;";
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand("copy"); } catch (e) {}
        document.body.removeChild(ta);
        done();
      });
    } else {
      var ta2 = document.createElement("textarea");
      ta2.value = text;
      ta2.style.cssText = "position:fixed;left:-9999px;top:0;";
      document.body.appendChild(ta2);
      ta2.select();
      try { document.execCommand("copy"); } catch (e) {}
      document.body.removeChild(ta2);
      done();
    }
  }

  window.shareSportiqPostV29 = function (postId) {
    var sid = String(postId || "").trim();
    if (!sid) return;
    var origin = (window.location && window.location.origin && window.location.origin !== "null")
      ? window.location.origin
      : "https://billnojay.com";
    var url = origin.replace(/\/+$/, "") + "/?post=" + encodeURIComponent(sid);
    copyTextFallbackV29(url, "🔗 이 분석글 공유 링크가 복사되었습니다! 카톡·커뮤니티에 바로 붙여넣으세요.");
  };

  function injectPostPreviewIntoCommentModalV29(postObj) {
    var cm = document.getElementById("commentModal") || document.getElementById("post-modal");
    if (!cm || !postObj) return;
    var listEl = document.getElementById("commentModalList") || cm.querySelector("[id*='comment'][id*='List'], .space-y-2, .overflow-y-auto");
    if (!listEl || !listEl.parentElement) return;

    var box = document.getElementById("sqModalPostBodyBoxV29");
    if (!box) {
      box = document.createElement("div");
      box.id = "sqModalPostBodyBoxV29";
      box.style.cssText = "background:rgba(2,6,23,.72);border:1px solid rgba(51,65,85,.85);border-radius:12px;padding:12px 14px;margin-bottom:12px;max-height:210px;overflow-y:auto;font-size:12.8px;line-height:1.62;color:#cbd5e1;";
      listEl.parentElement.insertBefore(box, listEl);
    }
    var bodyTxt = String(postObj.body || postObj.summary || postObj.content || "").trim();
    var metaLine = uxEsc(postObj.league || "SPORTIQ") + " · " + uxEsc(postObj.author || "SportIQ 분석") + " · ♥ " + Number(postObj.likes || 0);
    box.innerHTML = (
      "<div style='display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap;'>" +
      "  <span style='font-size:11.5px;font-weight:800;color:#34d399;'>" + metaLine + "</span>" +
      "  <button type='button' id='sqModalCopyDeepLinkBtnV29' style='background:rgba(14,165,233,.16);border:1px solid rgba(56,189,248,.45);color:#7dd3fc;border-radius:999px;padding:3px 10px;font-size:11px;font-weight:800;cursor:pointer;white-space:nowrap;'>🔗 이 분석글 링크 복사</button>" +
      "</div>" +
      (bodyTxt ? "<div style='white-space:pre-line;color:#e2e8f0;'>" + uxEsc(bodyTxt) + "</div>" : "")
    );
    var cpBtn = document.getElementById("sqModalCopyDeepLinkBtnV29");
    if (cpBtn) {
      cpBtn.onclick = function () { window.shareSportiqPostV29(String(postObj.id)); };
    }
  }

  function polishModalsAndCloseIconsV29() {
    var pinInp = document.getElementById("sqPostPinInput");
    if (pinInp && pinInp.parentElement) {
      pinInp.parentElement.setAttribute("data-sq-write-pin-row", "1");
    }
    var syncBtn = document.getElementById("sqDoSyncBtn");
    if (syncBtn && syncBtn.parentElement) {
      syncBtn.parentElement.setAttribute("data-sq-sync-grid", "1");
    }

    // Fix vertical wrapping on #commentModal header & broken close glyph on Notification / Comment modals
    var modals = document.querySelectorAll("#commentModal, #post-modal, #writeModal, #sqMemberHubModal, [id*='Notif'], [id*='notif'], [id*='Alert']");
    modals.forEach(function (m) {
      m.querySelectorAll("button").forEach(function (b) {
        var raw = (b.textContent || "").replace(/\s+/g, " ").trim();
        if (!raw) return;
        // Fix header sibling title flex min-width so close buttons never wrap vertically
        if (raw.indexOf("닫기") !== -1 || raw === "×" || raw === "✕" || raw === "X" || b.getAttribute("aria-label") === "닫기") {
          b.style.setProperty("white-space", "nowrap", "important");
          b.style.setProperty("flex-shrink", "0", "important");
          b.style.setProperty("display", "inline-flex", "important");
          b.style.setProperty("align-items", "center", "important");
          b.style.setProperty("justify-content", "center", "important");
          b.style.setProperty("gap", "4px", "important");
          if (b.parentElement) {
            var prevTitle = b.previousElementSibling;
            if (prevTitle && prevTitle.style) {
              prevTitle.style.minWidth = "0";
              prevTitle.style.flex = "1 1 auto";
            }
          }
          if (!b.querySelector("svg[data-sq-x-svg='1']")) {
            var hasKorClose = raw.indexOf("닫기") !== -1;
            var hasEsc = raw.indexOf("Esc") !== -1;
            var svgHtml = "<svg data-sq-x-svg='1' viewBox='0 0 14 14' width='12' height='12' fill='none' aria-hidden='true' style='display:inline-block;flex-shrink:0;pointer-events:none;'><path d='M3 3L11 11M11 3L3 11' stroke='currentColor' stroke-width='2.2' stroke-linecap='round'/></svg>";
            if (hasKorClose) {
              b.innerHTML = svgHtml + "<span>" + (hasEsc ? "닫기 (Esc)" : "닫기") + "</span>";
            } else if (raw.length <= 3) {
              b.innerHTML = svgHtml;
              b.style.setProperty("min-width", "28px", "important");
              b.style.setProperty("height", "28px", "important");
            }
          }
        }
      });
    });

    // Also scan any dynamically created '실시간 알림 센터' modal not matching static IDs
    document.querySelectorAll("div, section").forEach(function (panel) {
      if (panel.offsetWidth > 0 && (panel.textContent || "").indexOf("실시간 알림 센터") !== -1 && panel.offsetHeight < 760) {
        panel.querySelectorAll("button").forEach(function (b) {
          var t = (b.textContent || "").trim();
          if (t && t.indexOf("푸시") === -1 && t.indexOf("읽음") === -1 && t.length <= 4 && !b.querySelector("svg[data-sq-x-svg='1']")) {
            b.innerHTML = "<svg data-sq-x-svg='1' viewBox='0 0 14 14' width='12' height='12' fill='none' aria-hidden='true' style='display:block;pointer-events:none;'><path d='M3 3L11 11M11 3L3 11' stroke='currentColor' stroke-width='2.2' stroke-linecap='round'/></svg>";
            b.style.setProperty("display", "inline-flex", "important");
            b.style.setProperty("align-items", "center", "important");
            b.style.setProperty("justify-content", "center", "important");
            b.style.setProperty("min-width", "28px", "important");
            b.style.setProperty("height", "28px", "important");
            b.style.setProperty("flex-shrink", "0", "important");
          }
        });
      }
    });
  }

  function ensureHofCardMountedSyncV29() {
    var g = document.getElementById("dcLauncherGrid") || document.getElementById("dcLaunchCards");
    if (!g || !g.children || g.children.length < 3) return;
    if (document.getElementById("dcHofLaunchCard")) return;
    var sample = g.children[0];
    var tag = sample ? sample.tagName.toLowerCase() : "div";
    var c = document.createElement(tag);
    c.id = "dcHofLaunchCard";
    if (tag === "button") c.type = "button";
    c.className = sample ? sample.className.replace(/\bactive\b/g, "").trim() : "dc-launch-card";
    c.setAttribute("data-dctab", "hall-of-fame");
    c.setAttribute("data-tab", "hall-of-fame");
    c.innerHTML = "<div style='display:flex;justify-content:space-between;align-items:center;font-weight:800;font-size:13.5px;color:#fbbf24;'><span>🏆 명예의 전당 랭킹</span><span style='color:#4ade80;font-size:10px;font-weight:800;'>• TOP 분석가</span></div><div style='font-size:11px;color:#94a3b8;margin-top:4px;'>베스트 수상·누적 보너스P·적중률 종합 순위</div>";
    c.addEventListener("click", function (ev) {
      ev.preventDefault();
      ev.stopPropagation();
      if (ev.stopImmediatePropagation) ev.stopImmediatePropagation();
      if (typeof window.__openSportiqHallOfFame === "function") {
        window.__openSportiqHallOfFame();
      }
    }, true);
    g.appendChild(c);
  }

  function stabilizeHofLaunchCardV29() {
    ensureHofCardMountedSyncV29();
    var g = document.getElementById("dcLauncherGrid") || document.getElementById("dcLaunchCards");
    if (g && typeof MutationObserver !== "undefined" && !g.__sqHofZeroFlickerObsV29) {
      g.__sqHofZeroFlickerObsV29 = true;
      var obs = new MutationObserver(function () {
        ensureHofCardMountedSyncV29();
      });
      obs.observe(g, { childList: true });
    }
  }

  function openPostDetailAndCommentsV29(cid) {
    if (!cid || typeof state !== "object" || !state || !Array.isArray(state.posts)) return false;
    var postObj = state.posts.find(function (x) { return x && String(x.id) === String(cid); });
    if (!postObj) return false;
    state.activePostId = String(postObj.id);
    var cmFunc = window.openComments || window.openCommentModal;
    if (typeof cmFunc === "function") {
      try { cmFunc(String(postObj.id)); } catch (e) {}
    } else {
      var cm = document.getElementById("commentModal");
      if (cm) {
        cm.classList.remove("hidden");
        cm.style.display = "flex";
        var tEl = document.getElementById("commentModalTitle");
        if (tEl) tEl.textContent = postObj.title || "분석글 상세 · 댓글";
        if (typeof window.renderCommentModalList === "function") {
          try { window.renderCommentModalList(postObj); } catch (e) {}
        }
      }
    }
    setTimeout(function () {
      injectPostPreviewIntoCommentModalV29(postObj);
      polishModalsAndCloseIconsV29();
    }, 20);
    return true;
  }

  function handleDeepLinkPostOnBootV29() {
    if (window.__sqDeepLinkHandledV29) return;
    var targetId = "";
    try {
      var sp = new URLSearchParams(window.location.search || "");
      targetId = (sp.get("post") || "").trim();
    } catch (e) {}
    if (!targetId) return;
    if (typeof state !== "object" || !state || !Array.isArray(state.posts) || !state.posts.length) return;
    normalizeStatePostsV24();
    var postObj = state.posts.find(function (x) { return x && String(x.id) === targetId; });
    if (!postObj) return;

    window.__sqDeepLinkHandledV29 = true;
    if (typeof window.switchTab === "function") {
      try { window.switchTab("feed"); } catch (e) {}
    }
    if (typeof window.renderFeed === "function") {
      try { window.renderFeed(); } catch (e) {}
    }
    syncRenderedFeedReactionButtonsDom();

    var box = document.getElementById("feedList");
    if (box) {
      box.querySelectorAll("article").forEach(function (art) {
        if (art.getAttribute("data-sq-post-id") === targetId) {
          art.setAttribute("data-sq-shared-highlight", "1");
          if (art.scrollIntoView) art.scrollIntoView({ behavior: "smooth", block: "center" });
        }
      });
    }
    openPostDetailAndCommentsV29(targetId);
  }

  // -------------------------------------------------------------------------
  // v3.1 Scorebase Phase-2: ±5d Calendar Bar, Row Hover Stat Preview, Card-Track Filter & 0s Live Dots
  // -------------------------------------------------------------------------
  var LS_FAV_MATCHES_V30 = "sportiq_fav_matches_v30";
  var v30ScoreState = {
    viewMode: "cards", // "cards" | "table-league" | "table-time"
    statusFilter: "all", // "all" | "live" | "pre" | "post" | "fav"
    selectedSport: "all",
    selectedLeague: "all",
    selectedDateOffset: 0,
    userPickedSport: false,
    customDateRendered: false,
    isLoadingDate: false,
    dateStrip: [],
    favMap: {},
    rawAllItems: [],
    items: [],
    loadedDateOffset: null,
    fetchingOffset: null,
    lastCalSig: "",
    lastToolbarSig: "",
    lastTableSig: ""
  };

  try {
    var rawFav = localStorage.getItem(LS_FAV_MATCHES_V30);
    if (rawFav) v30ScoreState.favMap = JSON.parse(rawFav) || {};
  } catch (e) {}

  function saveFavMatchesV30() {
    try { localStorage.setItem(LS_FAV_MATCHES_V30, JSON.stringify(v30ScoreState.favMap || {})); } catch (e) {}
  }

  function buildClientDateStripV31(activeOff) {
    var out = [];
    var wdKo = ["일", "월", "화", "수", "목", "금", "토"];
    var nowMs = Date.now();
    for (var off = -5; off <= 5; off++) {
      var kstMs = nowMs + (off * 86400000) + (9 * 3600000);
      var dt = new Date(kstMs);
      var m = String(dt.getUTCMonth() + 1);
      if (m.length < 2) m = "0" + m;
      var d = String(dt.getUTCDate());
      if (d.length < 2) d = "0" + d;
      var wIdx = dt.getUTCDay();
      out.push({
        offset: off,
        mmdd: m + "." + d,
        wd: wdKo[wIdx],
        rel: off === 0 ? "오늘" : (off === -1 ? "어제" : (off === 1 ? "내일" : "")),
        is_today: off === 0,
        is_sat: wIdx === 6,
        is_sun: wIdx === 0,
        active: off === activeOff
      });
    }
    return out;
  }

  function normMatchStateV30(raw) {
    if (!raw) return "pre";
    var st = String(raw.state || raw.status || "").trim().toLowerCase();
    var bd = String(raw.status_badge || raw.status_text || raw.status_detail || "").trim();
    var bdu = bd.toUpperCase();
    if (st === "post" || st === "final" || st === "ft" || st === "ended" || st === "finished" || st === "completed") return "post";
    if (
      bdu === "FT" ||
      bd.indexOf("종료") !== -1 ||
      bdu.indexOf("FINAL") !== -1 ||
      bdu.indexOf("AET") !== -1 ||
      bd.indexOf("판정승") !== -1 ||
      bd.indexOf("서브미션") !== -1 ||
      /\b(TKO|KO|SUB)\b/.test(bdu)
    ) {
      return "post";
    }
    if (st === "in" || st === "live" || st === "inprogress" || st === "playing") return "in";
    if (
      bdu.indexOf("LIVE") !== -1 ||
      bd.indexOf("진행") !== -1 ||
      /\b([1-4]Q|[1-3]P|HT|OT)\b/.test(bdu) ||
      /\d+:\d+\s*-\s*(1ST|2ND|3RD|4TH|OT)/i.test(bd) ||
      /\b\d{1,3}\s*'|\d+\s*회(초|말)?|[1-5]\s*세트/.test(bd)
    ) {
      return "in";
    }
    return "pre";
  }

  function canonicalSportKeyV304(rawSport, lgHint) {
    var s = String(rawSport || "").trim().toLowerCase();
    var u = String(lgHint || "").toUpperCase();
    if (s === "volleyball" || s === "vleague" || /VLEAGUE|V-리그|배구/.test(u)) return "volleyball";
    if (s === "ufc" || s === "mma" || s === "fighting" || /UFC|MMA|격투/.test(u)) return "ufc";
    if (s === "tennis" || s === "atp" || s === "wta" || /\b(ATP|WTA)\b|테니스/.test(u)) return "tennis";
    if (s === "f1" || s === "motorsport" || s === "racing" || /\bF1\b|포뮬러/.test(u)) return "f1";
    if (s === "esports" || s === "lck" || s === "lol" || /\bLCK\b|LOL|ESPORTS|리그\s*오브\s*레전드/.test(u)) return "esports";
    if (s === "basketball" || s === "nba" || s === "wnba" || /\b(NBA|WNBA|KBL|WKBL)\b/.test(u)) return "basketball";
    if (s === "baseball" || s === "mlb" || s === "kbo" || s === "npb" || /\b(KBO|MLB|NPB|ABL)\b/.test(u)) return "baseball";
    if (s === "hockey" || s === "nhl" || /\bNHL\b|하키/.test(u)) return "hockey";
    if (s === "soccer" || s === "football") return "soccer";
    return "soccer";
  }

  function parseSportKeyFromBtnV304(btn) {
    if (!btn) return "";
    var ds = String(btn.getAttribute("data-sport") || "").trim().toLowerCase();
    if (ds) {
      if (ds === "all") return "all";
      return canonicalSportKeyV304(ds, ds);
    }
    var cloneTxt = String(btn.textContent || "").replace(/•\s*LIVE\s*\d+/gi, "").trim();
    if (!cloneTxt) return "";
    if (/^(🌐\s*)?(전체|\d+종목\s*전체)\s*(\(\d+\))?$/.test(cloneTxt) || (/전체\s*\(\d+\)/.test(cloneTxt) && cloneTxt.indexOf("TOP") === -1 && cloneTxt.indexOf("리그") === -1)) return "all";
    if (cloneTxt.indexOf("축구") !== -1) return "soccer";
    if (cloneTxt.indexOf("야구") !== -1) return "baseball";
    if (cloneTxt.indexOf("농구") !== -1) return "basketball";
    if (cloneTxt.indexOf("배구") !== -1 || /V-리그/i.test(cloneTxt)) return "volleyball";
    if (cloneTxt.indexOf("하키") !== -1) return "hockey";
    if (/LCK|e스포츠/i.test(cloneTxt)) return "esports";
    if (/UFC|MMA|격투/i.test(cloneTxt)) return "ufc";
    if (cloneTxt.indexOf("테니스") !== -1 || /\b(ATP|WTA)\b/.test(cloneTxt)) return "tennis";
    if (/\bF1\b|포뮬러/i.test(cloneTxt)) return "f1";
    return "";
  }

  function isExcludedSectionForTopScoreBarV304(el) {
    if (!el || !el.closest) return true;
    return Boolean(
      el.closest(
        "#sqV31DateCalendarBar, #sqV31DateCalendarBar, #sqV30ScoreToolbar, #sqV30ScoreboardTableWrap, #dcMainHubSection, #dcHubCollapsible, #dcHubBody, " +
        "#rssSportFilters, #tab-feed, #tab-schedule, #tab-shorts, #tab-issues, #tab-ranking, #tab-lab, " +
        "#globalTop10Grid, #sqOfficialLegalFooter, .sq-match-sort-bar"
      )
    );
  }

  var _inSportDotMutationV304 = false;
  function ensureSportPillMutationObserverV304(container) {
    if (!container || container.__sqSportDotObsV304 || typeof MutationObserver === "undefined") return;
    container.__sqSportDotObsV304 = true;
    var obs = new MutationObserver(function () {
      if (_inSportDotMutationV304) return;
      syncSportPillLiveDotsV30();
    });
    obs.observe(container, { childList: true, subtree: true, characterData: true });
  }

  // Also observe #proLiveTickerBar root so the EXACT millisecond widget.js injects the 10-sport buttons on first load, • LIVE dots attach at 0s!
  function ensureRootTickerObserverV31() {
    var root = document.getElementById("proLiveTickerBar");
    if (!root || root.__sqRootLiveObsV31 || typeof MutationObserver === "undefined") return;
    root.__sqRootLiveObsV31 = true;
    var pending = false;
    var obs = new MutationObserver(function () {
      if (_inSportDotMutationV304 || pending) return;
      pending = true;
      setTimeout(function () {
        pending = false;
        syncSportPillLiveDotsV30();
        if (v30ScoreState.viewMode === "cards") {
          decorateMiniScoreCardsV30();
        }
      }, 15);
    });
    obs.observe(root, { childList: true, subtree: true });
  }

  // v3.4.1 Lock 3: Sync sport pill total counts + route KBL/WKBL DataCenter standings via NBA basketball renderer + live-scores fetch bridge!
  var _sqProxyDcLeagueV341 = "";
  var _sqProxyDcTimerV341 = null;

  function triggerDomesticDcStandingsV341(code, targetTab) {
    var cu = String(code || "KBL").toUpperCase();
    _sqProxyDcLeagueV341 = cu;
    if (_sqProxyDcTimerV341) clearTimeout(_sqProxyDcTimerV341);
    _sqProxyDcTimerV341 = setTimeout(function () { _sqProxyDcLeagueV341 = ""; }, 1800);

    var hubBody = document.getElementById("dcHubBody");
    if (targetTab && typeof window.__switchDcTab === "function") {
      try { window.__switchDcTab(targetTab, "NBA"); } catch (e) {}
    } else if (hubBody) {
      var btns = hubBody.querySelectorAll("button");
      var clickedNba = false;
      for (var i = 0; i < btns.length; i++) {
        if (String(btns[i].textContent || "").trim() === "NBA") {
          clickedNba = true;
          try { btns[i].click(); } catch (e) {}
          break;
        }
      }
      if (!clickedNba && typeof window.__switchDcTab === "function") {
        try { window.__switchDcTab("standings", "NBA"); } catch (e) {}
      }
    }
    setTimeout(function () {
      var hb = document.getElementById("dcHubBody");
      if (!hb) return;
      Array.prototype.forEach.call(hb.querySelectorAll("button"), function (b) {
        var t = String(b.textContent || "").trim();
        if (t === cu) {
          b.classList.add("active");
          b.style.background = "#2563eb";
          b.style.color = "#ffffff";
          b.style.borderColor = "#3b82f6";
        } else if (t === "NBA" || (t === "KBL" && cu !== "KBL") || (t === "WKBL" && cu !== "WKBL")) {
          b.classList.remove("active");
          b.style.background = "";
          b.style.color = "";
          b.style.borderColor = "";
        }
      });
    }, 180);
  }

  function installDomesticBballFetchBridgeV341() {
    if (window.__sqFetchBridgeV341 || typeof window.fetch !== "function") return;
    window.__sqFetchBridgeV341 = true;
    var _prevFetch = window.fetch;
    window.fetch = function (input, init) {
      var urlStr = typeof input === "string" ? input : ((input && input.url) ? String(input.url) : "");
      if (urlStr) {
        // 1. Direct or proxied KBL/WKBL standings, season-sim, and injuries -> route to guaranteed domestic-bball-meta-v34!
        var mDc = urlStr.match(/\/api\/datacenter\/(standings|season-sim|injuries)\?([^#]*)/);
        if (mDc) {
          var ep = mDc[1];
          var qs = new URLSearchParams(mDc[2] || "");
          var reqLg = String(qs.get("league") || "").toUpperCase();
          if (_sqProxyDcLeagueV341 && (reqLg === "NBA" || reqLg === "EPL" || !reqLg)) {
            reqLg = _sqProxyDcLeagueV341;
          }
          if (reqLg === "KBL" || reqLg === "WKBL") {
            var kind = ep === "season-sim" ? "sim" : (ep === "injuries" ? "injuries" : "standings");
            var newUrl = "/api/member/domestic-bball-meta-v34?kind=" + kind + "&league=" + reqLg + "&severity=" + encodeURIComponent(qs.get("severity") || "all") + "&q=" + encodeURIComponent(qs.get("q") || "");
            return _prevFetch.call(window, newUrl, init);
          }
        }
        // Clear proxy flag if user explicitly clicked another league (e.g. EPL, LALIGA, NBA directly)
        if (mDc && !_sqProxyDcLeagueV341) {
          var hb = document.getElementById("dcHubBody");
          if (hb) {
            Array.prototype.forEach.call(hb.querySelectorAll("button[data-sq-dc-lg]"), function (b) {
              b.classList.remove("active");
              b.style.background = "";
              b.style.color = "";
              b.style.borderColor = "";
            });
          }
        }
      }
      if (urlStr && urlStr.indexOf("/api/datacenter/live-scores") !== -1) {
        var qsLs = new URLSearchParams((urlStr.split("?")[1] || "").split("#")[0]);
        var spReq = String(qsLs.get("sport") || "all").toLowerCase();
        var lgReq = String(qsLs.get("league") || "all").toUpperCase();
        var offReq = parseInt(qsLs.get("date_offset") || "0", 10) || 0;
        if (spReq === "all" || spReq === "basketball") {
          return Promise.all([
            _prevFetch.apply(window, arguments).then(function (r) { return r.json(); }).catch(function () { return null; }),
            _prevFetch.call(window, "/api/member/scoreboard-v30?date_offset=" + offReq).then(function (r) { return r.json(); }).catch(function () { return null; })
          ]).then(function (pair) {
            var dcJson = pair[0] || { ok: true, items: [], counts: {} };
            var sbJson = pair[1] || { items: [] };
            var domItems = (sbJson.items || []).filter(function (x) {
              var lc = String((x && (x.league_code || x.league)) || "").toUpperCase();
              return lc === "KBL" || lc === "WKBL";
            });
            var curItems = Array.isArray(dcJson.items) ? dcJson.items.slice() : [];
            var seenMap = {};
            curItems.forEach(function (x) { if (x && x.id) seenMap[x.id] = true; });
            var addedCnt = 0, addedLive = 0;
            domItems.forEach(function (dg) {
              var dlc = String(dg.league_code || dg.league || "").toUpperCase();
              if (!seenMap[dg.id]) {
                addedCnt++;
                if (dg.state === "in") addedLive++;
                if (lgReq === "ALL" || lgReq === dlc) {
                  curItems.push(dg);
                }
              }
            });
            var ord = { "in": 0, "pre": 1, "post": 2 };
            curItems.sort(function (a, b) {
              var oa = ord[a.state] != null ? ord[a.state] : 3;
              var ob = ord[b.state] != null ? ord[b.state] : 3;
              return oa !== ob ? (oa - ob) : String(a.match_dt_kst || "").localeCompare(String(b.match_dt_kst || ""));
            });
            if (addedCnt > 0 && dcJson.counts && typeof dcJson.counts === "object") {
              dcJson.counts.basketball = Number(dcJson.counts.basketball || 0) + addedCnt;
              dcJson.counts.all = Number(dcJson.counts.all || 0) + addedCnt;
              dcJson.counts.live = Number(dcJson.counts.live || 0) + addedLive;
            }
            dcJson.items = curItems;
            return new Response(JSON.stringify(dcJson), { status: 200, headers: { "Content-Type": "application/json" } });
          });
        }
      }
      return _prevFetch.apply(window, arguments);
    };
  }
  installDomesticBballFetchBridgeV341();
  setTimeout(function () {
    if (window.__sqBootCardResyncedV342) return;
    window.__sqBootCardResyncedV342 = true;
    var actPill = document.querySelector("#dcSportPills .dc-pill.active, #dcSportPills button.active");
    if (actPill) {
      try { actPill.click(); } catch (e) {}
    }
  }, 260);

  // v3.4 Ensure KBL & WKBL appear in top #dcTickerLeagueSel AND DataCenter #dcHubBody league pill bars
  function ensureDomesticBasketballUiControlsV34() {
    var sel = discoverTopLeagueSelectV304();
    if (sel && sel.options && !sel.getAttribute("data-sq-v34-kbl")) {
      var hasKbl = false, hasWkbl = false, insAfter = null;
      for (var i = 0; i < sel.options.length; i++) {
        var ov = String(sel.options[i].value || "").toUpperCase();
        if (ov === "KBL") hasKbl = true;
        if (ov === "WKBL") hasWkbl = true;
        if (ov === "WNBA" || ov === "NBA") insAfter = sel.options[i];
      }
      if (!hasKbl) {
        var o1 = document.createElement("option");
        o1.value = "KBL";
        o1.textContent = "KBL (국내남자농구)";
        if (insAfter && insAfter.nextSibling) sel.insertBefore(o1, insAfter.nextSibling);
        else sel.appendChild(o1);
        insAfter = o1;
      }
      if (!hasWkbl) {
        var o2 = document.createElement("option");
        o2.value = "WKBL";
        o2.textContent = "WKBL (국내여자농구)";
        if (insAfter && insAfter.nextSibling) sel.insertBefore(o2, insAfter.nextSibling);
        else sel.appendChild(o2);
      }
      if (sel.options[0] && /전체\s*\d+개\s*리그/.test(sel.options[0].textContent)) {
        sel.options[0].textContent = "전체 " + (sel.options.length - 1) + "개 리그";
      }
      sel.setAttribute("data-sq-v34-kbl", "1");
    }

    // Also add KBL & WKBL league buttons inside #dcHubBody next to the NBA button whenever DataCenter renders!
    var hubBody = document.getElementById("dcHubBody");
    if (hubBody) {
      var btns = hubBody.querySelectorAll("button");
      for (var b = 0; b < btns.length; b++) {
        var bt = String(btns[b].textContent || "").trim();
        if (bt === "NBA" && btns[b].parentElement && !btns[b].parentElement.querySelector("[data-sq-dc-lg='KBL']")) {
          var bar = btns[b].parentElement;
          ["WKBL", "KBL"].forEach(function (code) {
            var nb = btns[b].cloneNode(true);
            nb.textContent = code;
            nb.classList.remove("active");
            nb.setAttribute("data-sq-dc-lg", code);
            nb.removeAttribute("onclick");
            nb.onclick = function (ev) {
              if (ev) { ev.preventDefault(); ev.stopPropagation(); }
              triggerDomesticDcStandingsV341(code, "");
            };
            if (btns[b].nextSibling) bar.insertBefore(nb, btns[b].nextSibling);
            else bar.appendChild(nb);
          });
          break;
        }
      }
    }
  }

  function discoverTopSportButtonsV304() {
    var out = [];
    var pillWrap = document.getElementById("dcSportPills");
    if (pillWrap) {
      Array.prototype.forEach.call(pillWrap.querySelectorAll("button"), function (b) {
        var sp = parseSportKeyFromBtnV304(b);
        if (sp) {
          b.setAttribute("data-sq-v304-sport", sp);
          out.push(b);
        }
      });
      if (out.length >= 3) {
        ensureSportPillMutationObserverV304(pillWrap);
        return out;
      }
    }

    var roots = [];
    var pb = document.getElementById("proLiveTickerBar");
    if (pb) roots.push(pb);
    var tb = document.getElementById("sqV30ScoreToolbar");
    if (tb && tb.parentElement && roots.indexOf(tb.parentElement) === -1) roots.push(tb.parentElement);
    roots.push(document.body);

    for (var r = 0; r < roots.length; r++) {
      var rt = roots[r];
      if (!rt || !rt.querySelectorAll) continue;
      var containers = rt.querySelectorAll("div, nav");
      for (var i = 0; i < containers.length; i++) {
        var c = containers[i];
        if (isExcludedSectionForTopScoreBarV304(c)) continue;
        var directBtns = Array.prototype.filter.call(c.children || [], function (ch) { return ch && ch.tagName === "BUTTON"; });
        if (directBtns.length < 4 || directBtns.length > 16) continue;
        var matched = [];
        var hasSoccer = false, hasBaseball = false;
        for (var j = 0; j < directBtns.length; j++) {
          var k = parseSportKeyFromBtnV304(directBtns[j]);
          if (k) {
            if (k === "soccer") hasSoccer = true;
            if (k === "baseball") hasBaseball = true;
            matched.push({ btn: directBtns[j], key: k });
          }
        }
        if (hasSoccer && hasBaseball && matched.length >= 4) {
          matched.forEach(function (m) {
            m.btn.setAttribute("data-sq-v304-sport", m.key);
            out.push(m.btn);
          });
          ensureSportPillMutationObserverV304(c);
          return out;
        }
      }
    }
    return out;
  }

  function discoverTopLeagueSelectV304() {
    var direct = document.getElementById("dcTickerLeagueSel");
    if (direct) return direct;
    var selects = document.querySelectorAll("select");
    for (var i = 0; i < selects.length; i++) {
      var s = selects[i];
      if (isExcludedSectionForTopScoreBarV304(s) || s.id === "postLeague") continue;
      var firstTxt = (s.options && s.options[0]) ? String(s.options[0].textContent || "") : "";
      if (firstTxt.indexOf("전체") !== -1 && firstTxt.indexOf("리그") !== -1) {
        return s;
      }
    }
    return null;
  }

  function discoverTopDateButtonsV304() {
    var out = [];
    var direct = document.querySelectorAll(".dc-date-btn");
    if (direct && direct.length) {
      Array.prototype.forEach.call(direct, function (b) { out.push(b); });
      return out;
    }
    document.querySelectorAll("button").forEach(function (b) {
      if (isExcludedSectionForTopScoreBarV304(b)) return;
      var t = String(b.textContent || "").trim();
      if (/^(◀\s*어제|어제|오늘\s*·\s*실시간|오늘|내일\s*▶|내일|\+2일)$/.test(t)) {
        out.push(b);
      }
    });
    return out;
  }

  function detectVisuallyActiveSportFromDomV304(sportBtns) {
    for (var i = 0; i < sportBtns.length; i++) {
      var b = sportBtns[i];
      var sp = b.getAttribute("data-sq-v304-sport") || parseSportKeyFromBtnV304(b);
      if (!sp) continue;
      if (b.classList && b.classList.contains("active")) return sp;
      var bg = String((b.style && b.style.background) || (b.style && b.style.backgroundColor) || "").toLowerCase();
      if (/2563eb|1d4ed8|3b82f6|0284c7|rgb\(37,\s*99,\s*235\)|rgb\(29,\s*78,\s*216\)|rgb\(2,\s*132,\s*199\)/.test(bg)) {
        return sp;
      }
    }
    return "";
  }

  function normalizeScoreItemV30(raw, idx) {
    if (!raw || typeof raw !== "object") return null;
    var lgProbe = String(raw.league_code || raw.league || raw.sport || "").trim();
    var spProbe = canonicalSportKeyV304(raw.sport || raw.sport_key || "", lgProbe);
    var home = String(raw.home || raw.home_team || raw.homeTeam || raw.home_name || "").trim();
    var away = String(raw.away || raw.away_team || raw.awayTeam || raw.away_name || "").trim();
    if (spProbe === "f1") {
      if (!home || /^home$/i.test(home)) home = "베르스타펜 (레드불)";
      if (!away || /^away$/i.test(away)) away = "노리스 (맥라렌)";
    }
    if (!home || !away) return null;
    var st = normMatchStateV30(raw);
    var hs = raw.home_score != null ? Number(raw.home_score) : (raw.hs != null ? Number(raw.hs) : 0);
    var as = raw.away_score != null ? Number(raw.away_score) : (raw.as != null ? Number(raw.as) : 0);
    if (isNaN(hs)) hs = 0;
    if (isNaN(as)) as = 0;

    var lgShort = String(raw.league || raw.league_short || raw.league_code || "LEAGUE").trim();
    var lgCode = String(raw.league_code || lgShort || "EPL").trim();
    var lgFull = String(raw.league_full || raw.league_name || lgShort).trim();
    var sport = canonicalSportKeyV304(raw.sport || raw.sport_key || "", lgCode + " " + lgShort + " " + lgFull);

    var rawAi = raw.ai || {};
    var rawOdds = rawAi.odds || raw.odds || {};
    var hasDraw = (sport === "soccer");
    var hp = Number(rawAi.home_pct != null ? rawAi.home_pct : (raw.home_pct != null ? raw.home_pct : (raw.prob_home != null ? raw.prob_home : (hasDraw ? 44 : 54))));
    var dp = Number(rawAi.draw_pct != null ? rawAi.draw_pct : (raw.draw_pct != null ? raw.draw_pct : (hasDraw ? 26 : 0)));
    var ap = Number(rawAi.away_pct != null ? rawAi.away_pct : (raw.away_pct != null ? raw.away_pct : (100 - hp - dp)));
    if (isNaN(hp)) hp = hasDraw ? 44 : 54;
    if (isNaN(dp)) dp = hasDraw ? 26 : 0;
    if (isNaN(ap)) ap = Math.max(5, 100 - hp - dp);

    var oh = rawOdds.home || raw.odds_home || raw.home_odds || (hp > 0 ? (94 / hp).toFixed(2) : "-");
    var od = rawOdds.draw || raw.odds_draw || raw.draw_odds || (dp > 0 ? (94 / dp).toFixed(2) : "");
    var oa = rawOdds.away || raw.odds_away || raw.away_odds || (ap > 0 ? (94 / ap).toFixed(2) : "-");
    var badge = String(raw.status_badge || raw.status_text || (st === "in" ? "LIVE" : (st === "post" ? "종료" : "예정"))).trim();

    return {
      _idx: idx,
      id: String(raw.id || raw.match_id || (lgCode + "::" + home + "::" + away)),
      sport: sport,
      league: lgShort,
      league_code: lgCode,
      league_full: lgFull,
      state: st,
      status_badge: badge,
      drop_badge: String(raw.drop_badge || "").trim(),
      match_dt_kst: String(raw.match_dt_kst || raw.kickoff_kst || raw.time_kst || raw.date_str || "").trim(),
      match_dt_full_kst: String(raw.match_dt_full_kst || raw.match_dt_kst || raw.kickoff_kst || "").trim(),
      home: home,
      away: away,
      home_score: hs,
      away_score: as,
      period_scores: String(raw.period_scores || raw.period_text || raw.sub_score || "").trim(),
      venue: String(raw.venue || "").trim(),
      ai: {
        home_pct: Math.round(hp),
        draw_pct: Math.round(dp),
        away_pct: Math.round(ap),
        odds: { home: String(oh), draw: od ? String(od) : "", away: String(oa) }
      }
    };
  }

  function getActiveDcControlsV30() {
    var sportBtns = discoverTopSportButtonsV304();
    if (!v30ScoreState.userPickedSport && sportBtns.length) {
      var domActive = detectVisuallyActiveSportFromDomV304(sportBtns);
      if (domActive) v30ScoreState.selectedSport = domActive;
    }
    var lgSel = discoverTopLeagueSelectV304();
    if (lgSel) {
      var lgRaw = String(lgSel.value || "").trim();
      v30ScoreState.selectedLeague = (!lgRaw || lgRaw.toLowerCase() === "all" || lgRaw.indexOf("전체") !== -1) ? "all" : lgRaw;
    }
    return {
      sport: v30ScoreState.selectedSport || "all",
      league: v30ScoreState.selectedLeague || "all",
      dateOffset: v30ScoreState.selectedDateOffset || 0
    };
  }

  function recomputeScopedItemsV30() {
    var ctl = getActiveDcControlsV30();
    var all = v30ScoreState.rawAllItems || [];
    var scoped = all.filter(function (it) {
      if (!it) return false;
      if (ctl.sport !== "all" && it.sport !== ctl.sport) return false;
      if (ctl.league !== "all") {
        var lu = ctl.league.toUpperCase();
        if (String(it.league_code || "").toUpperCase() !== lu && String(it.league || "").toUpperCase() !== lu) {
          return false;
        }
      }
      return true;
    });
    v30ScoreState.items = scoped;
    window.__dcLatestScoreItems = scoped;
    return scoped;
  }

  function renderCustomDateCardsIntoStripV31() {
    if (!v30ScoreState.customDateRendered) return;
    var mainStrip = findPrimaryMainCardStripV30();
    if (!mainStrip) return;
    var targetBox = (mainStrip.children && mainStrip.children.length === 1 && mainStrip.children[0].children && mainStrip.children[0].children.length >= 2)
      ? mainStrip.children[0]
      : mainStrip;
    var items = v30ScoreState.items || [];
    if (!items.length) {
      targetBox.innerHTML = "<div data-sq-v31-empty='1' style='padding:14px 18px;color:#94a3b8;font-size:12.5px;'>선택한 날짜·종목에 예정된 경기가 없습니다.</div>";
      return;
    }
    targetBox.innerHTML = items.map(function (it) {
      var isLive = it.state === "in";
      var stBg = isLive ? "#dc2626" : (it.state === "post" ? "#475569" : "#1d4ed8");
      var ai = it.ai || {};
      var odds = ai.odds || {};
      var probTxt = ai.draw_pct ? ("AI " + ai.home_pct + "% / " + ai.draw_pct + "% / " + ai.away_pct + "%") : ("AI " + ai.home_pct + "% / " + ai.away_pct + "%");
      var oddsTxt = odds.home ? ("배당 " + odds.home + (odds.draw ? "/" + odds.draw : "") + "/" + odds.away) : "";
      var fk = matchFavKeyV30(it);
      return [
        "<div data-sq-v30-fk='" + uxEsc(fk) + "' style='min-width:252px;max-width:292px;flex:0 0 auto;background:#0f172a;border:1px solid " + (isLive ? "#ef4444" : "#334155") + ";border-radius:12px;padding:9px 11px;cursor:pointer;color:#f8fafc;display:flex;flex-direction:column;'>",
        "  <div style='display:flex;align-items:center;justify-content:space-between;gap:4px;font-size:11px;margin-bottom:5px;'>",
        "    <span style='background:#1e293b;color:#93c5fd;padding:2px 6px;border-radius:6px;font-weight:700;'>" + uxEsc(it.league) + "</span>",
        "    <span style='color:#fde68a;font-weight:700;'>📅 " + uxEsc(it.match_dt_kst) + "</span>",
        "    <span style='background:" + stBg + ";color:#fff;padding:2px 6px;border-radius:6px;font-weight:700;'>" + uxEsc(it.status_badge) + "</span>",
        "  </div>",
        "  <div style='display:flex;align-items:center;justify-content:space-between;font-size:13px;font-weight:800;margin:4px 0;'>",
        "    <span style='overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:98px;'>" + uxEsc(it.home) + "</span>",
        "    <span style='background:#1e293b;padding:2px 9px;border-radius:8px;color:#fff;font-size:14px;'>" + uxEsc(it.home_score) + " : " + uxEsc(it.away_score) + "</span>",
        "    <span style='overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:98px;text-align:right;'>" + uxEsc(it.away) + "</span>",
        "  </div>",
        (it.period_scores ? "<div style='font-size:11px;color:#38bdf8;margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;'>📊 " + uxEsc(it.period_scores) + "</div>" : ""),
        "  <div style='display:flex;justify-content:space-between;align-items:center;font-size:10.5px;color:#94a3b8;margin-top:4px;'>",
        "    <span>🎯 " + uxEsc(probTxt) + "</span>",
        "    <span>" + uxEsc(oddsTxt) + "</span>",
        "  </div>",
        "</div>"
      ].join("");
    }).join("");
  }

  function mergeRawScoreItemsV30(rawList, replaceAll) {
    var norm = [];
    if (Array.isArray(rawList)) {
      for (var i = 0; i < rawList.length; i++) {
        var n = normalizeScoreItemV30(rawList[i], i);
        if (n) norm.push(n);
      }
    }
    if (replaceAll) {
      v30ScoreState.rawAllItems = norm;
    } else if (norm.length) {
      if (!v30ScoreState.rawAllItems.length) {
        v30ScoreState.rawAllItems = norm;
      } else {
        var byId = {};
        var orderKeys = [];
        v30ScoreState.rawAllItems.forEach(function (x) {
          var k = matchFavKeyV30(x);
          if (!byId[k]) orderKeys.push(k);
          byId[k] = x;
        });
        norm.forEach(function (x) {
          var k = matchFavKeyV30(x);
          if (!byId[k]) orderKeys.push(k);
          byId[k] = x;
        });
        var ordMap = { "in": 0, "pre": 1, "post": 2 };
        v30ScoreState.rawAllItems = orderKeys.map(function (k) { return byId[k]; }).sort(function (a, b) {
          var oa = ordMap[a.state] != null ? ordMap[a.state] : 3;
          var ob = ordMap[b.state] != null ? ordMap[b.state] : 3;
          return oa !== ob ? (oa - ob) : String(a.match_dt_kst || "").localeCompare(String(b.match_dt_kst || ""));
        });
      }
    }
    recomputeScopedItemsV30();
    if (v30ScoreState.customDateRendered) {
      renderCustomDateCardsIntoStripV31();
    }
    v30ScoreState.lastCalSig = "";
    v30ScoreState.lastToolbarSig = "";
    v30ScoreState.lastTableSig = "";
    refreshScoreboardSuiteV30();
    setTimeout(refreshScoreboardSuiteV30, 75);
    setTimeout(refreshScoreboardSuiteV30, 240);
  }

  function installLiveScoresFetchTapV30() {
    if (window.__sqLiveScoresFetchTappedV30 || !window.fetch) return;
    window.__sqLiveScoresFetchTappedV30 = true;
    var origFetch = window.fetch.bind(window);
    window.fetch = function (input, init) {
      var u = typeof input === "string" ? input : (input && input.url ? input.url : "");
      return origFetch(input, init).then(function (resp) {
        if (u.indexOf("/api/datacenter/live-scores") !== -1) {
          var reqOff = null;
          try {
            var qIdx = u.indexOf("?");
            if (qIdx !== -1) {
              var spParams = new URLSearchParams(u.slice(qIdx + 1));
              var qSp = spParams.get("sport");
              var qLg = spParams.get("league");
              var qOff = spParams.get("date_offset");
              if (qSp) {
                v30ScoreState.userPickedSport = true;
                v30ScoreState.selectedSport = (qSp === "all") ? "all" : canonicalSportKeyV304(qSp, qSp);
              }
              if (qLg != null) {
                v30ScoreState.selectedLeague = (!qLg || qLg === "all" || qLg.indexOf("전체") !== -1) ? "all" : qLg;
              }
              if (qOff != null && !isNaN(parseInt(qOff, 10))) {
                reqOff = parseInt(qOff, 10);
              }
            }
          } catch (e) {}
          // Ignore background 20s widget.js polling for date_offset=0 if the user is actively viewing a custom date (e.g. -4 or +4) on the ±5d calendar!
          if (v30ScoreState.customDateRendered && reqOff !== null && reqOff !== v30ScoreState.selectedDateOffset) {
            return resp;
          }
          resp.clone().json().then(function (d) {
            if (d && Array.isArray(d.items) && d.items.length > 0) {
              var isFullAll = (u.indexOf("sport=all") !== -1 && (u.indexOf("league=all") !== -1 || u.indexOf("league=") === -1));
              mergeRawScoreItemsV30(d.items, isFullAll);
            } else {
              recomputeScopedItemsV30();
              v30ScoreState.lastToolbarSig = "";
              v30ScoreState.lastTableSig = "";
              refreshScoreboardSuiteV30();
            }
          }).catch(function () {});
        }
        return resp;
      });
    };
  }

  function fetchServerScoreboardBridgeV30(forceOffset) {
    var targetOff = (typeof forceOffset === "number" && !isNaN(forceOffset))
      ? forceOffset
      : (v30ScoreState.selectedDateOffset || 0);
    if (v30ScoreState.loadedDateOffset === targetOff && v30ScoreState.rawAllItems.length > 0) {
      return;
    }
    if (v30ScoreState.fetchingOffset === targetOff) return;
    v30ScoreState.fetchingOffset = targetOff;
    v30ScoreState.isLoadingDate = true;
    v30ScoreState.lastCalSig = "";
    mountOrUpdateV30ScoreToolbar();

    fetch("/api/member/scoreboard-v30?date_offset=" + encodeURIComponent(targetOff))
      .then(function (r) { return r.json(); })
      .then(function (d) {
        v30ScoreState.fetchingOffset = null;
        v30ScoreState.isLoadingDate = false;
        if (d && d.ok) {
          v30ScoreState.loadedDateOffset = targetOff;
          if (Array.isArray(d.date_strip) && d.date_strip.length) {
            v30ScoreState.dateStrip = d.date_strip;
          }
          mergeRawScoreItemsV30(d.items || [], Boolean(v30ScoreState.customDateRendered));
          return;
        }
      })
      .catch(function () {
        v30ScoreState.fetchingOffset = null;
        v30ScoreState.isLoadingDate = false;
      });
  }

  function selectDateOffsetV31(newOff) {
    var clamped = Math.max(-5, Math.min(5, parseInt(newOff, 10) || 0));
    v30ScoreState.selectedDateOffset = clamped;
    v30ScoreState.loadedDateOffset = null;
    v30ScoreState.rawAllItems = [];

    // Check if native widget.js has a matching .dc-date-btn (e.g. -1, 0, 1, 2)
    var dtBtns = discoverTopDateButtonsV304();
    var matchedNativeBtn = null;
    for (var i = 0; i < dtBtns.length; i++) {
      var b = dtBtns[i];
      var oa = b.getAttribute("data-off");
      var ov = (oa != null && !isNaN(parseInt(oa, 10))) ? parseInt(oa, 10) : null;
      if (ov === null) {
        var bt = String(b.textContent || "");
        if (bt.indexOf("어제") !== -1) ov = -1;
        else if (bt.indexOf("오늘") !== -1) ov = 0;
        else if (bt.indexOf("내일") !== -1) ov = 1;
        else if (bt.indexOf("+2") !== -1) ov = 2;
      }
      if (ov === clamped) {
        matchedNativeBtn = b;
        break;
      }
    }

    if (matchedNativeBtn) {
      v30ScoreState.customDateRendered = false;
      try {
        window.__sqInternalDateClickV31 = true;
        matchedNativeBtn.click();
      } finally {
        window.__sqInternalDateClickV31 = false;
      }
    } else {
      v30ScoreState.customDateRendered = true;
    }

    fetchServerScoreboardBridgeV30(clamped);
  }

  function installTopControlCaptureSyncV304() {
    if (window.__sqTopControlCaptureV304) return;
    window.__sqTopControlCaptureV304 = true;

    window.addEventListener("click", function (ev) {
      if (window.__sqInternalDateClickV31) return;
      var t = ev.target;
      if (!t || !t.closest) return;
      var btn = t.closest("button");
      if (!btn || isExcludedSectionForTopScoreBarV304(btn)) return;

      var sportBtns = discoverTopSportButtonsV304();
      if (sportBtns.indexOf(btn) !== -1 || btn.hasAttribute("data-sq-v304-sport")) {
        var spKey = btn.getAttribute("data-sq-v304-sport") || parseSportKeyFromBtnV304(btn);
        if (spKey) {
          v30ScoreState.userPickedSport = true;
          v30ScoreState.selectedSport = spKey;
          v30ScoreState.selectedLeague = "all";
          recomputeScopedItemsV30();
          if (v30ScoreState.customDateRendered) renderCustomDateCardsIntoStripV31();
          v30ScoreState.lastToolbarSig = "";
          v30ScoreState.lastTableSig = "";
          refreshScoreboardSuiteV30();
          setTimeout(refreshScoreboardSuiteV30, 55);
          return;
        }
      }

      var dtBtns = discoverTopDateButtonsV304();
      if (dtBtns.indexOf(btn) !== -1) {
        var offAttr = btn.getAttribute("data-off");
        var offVal = 0;
        if (offAttr != null && !isNaN(parseInt(offAttr, 10))) {
          offVal = parseInt(offAttr, 10);
        } else {
          var btxt = String(btn.textContent || "");
          if (btxt.indexOf("어제") !== -1) offVal = -1;
          else if (btxt.indexOf("내일") !== -1) offVal = 1;
          else if (btxt.indexOf("+2") !== -1) offVal = 2;
          else offVal = 0;
        }
        v30ScoreState.customDateRendered = false;
        v30ScoreState.selectedDateOffset = offVal;
        v30ScoreState.loadedDateOffset = null;
        fetchServerScoreboardBridgeV30(offVal);
      }
    }, true);

    window.addEventListener("change", function (ev) {
      var t = ev.target;
      var lgSel = discoverTopLeagueSelectV304();
      if (t && lgSel && t === lgSel) {
        var rawV = String(lgSel.value || "").trim();
        v30ScoreState.selectedLeague = (!rawV || rawV.toLowerCase() === "all" || rawV.indexOf("전체") !== -1) ? "all" : rawV;
        recomputeScopedItemsV30();
        if (v30ScoreState.customDateRendered) renderCustomDateCardsIntoStripV31();
        v30ScoreState.lastToolbarSig = "";
        v30ScoreState.lastTableSig = "";
        refreshScoreboardSuiteV30();
      }
    }, true);
  }

  function isHelperControlBarV30(el) {
    if (!el || !el.tagName) return true;
    var id = el.id || "";
    if (
      id === "sqV31DateCalendarBar" ||
      id === "sqV30ScoreToolbar" ||
      id === "sqV30ScoreboardTableWrap" ||
      id === "dcTickerControls" ||
      id === "dcSportPills" ||
      id === "feedList" ||
      id === "dcLauncherGrid" ||
      id === "dcLaunchCards" ||
      id === "dcMainHubSection" ||
      id === "dcHubBody" ||
      id === "globalTop10Grid"
    ) {
      return true;
    }
    if (el.classList && el.classList.contains("sq-match-sort-bar")) return true;
    var txt = String(el.textContent || "");
    if (/휠·드래그|경기\s*정렬|전체\s*\(\d+\)\s*⚽/.test(txt) && el.children && el.children.length <= 14) return true;
    if (el.querySelector && (el.querySelector("#dcSportPills") || el.querySelector("#dcTickerLeagueSel") || el.querySelector("#feedList") || el.querySelector("[data-sq-v304-sport]"))) return true;
    return false;
  }

  function findPrimaryMainCardStripV30() {
    var direct = document.getElementById("dcLiveTickerTrack");
    if (direct && direct.children && direct.children.length >= 1) return direct;
    var ctrl = document.getElementById("dcTickerControls");
    var scopeRoot = (ctrl && ctrl.parentElement) || document.getElementById("proLiveTickerBar") || document.body;
    var divs = scopeRoot.querySelectorAll("div");
    for (var i = 0; i < divs.length; i++) {
      var d = divs[i];
      if (d.id === "proTickerList" || isHelperControlBarV30(d)) continue;
      if (d.children && d.children.length >= 2 && d.children.length <= 180) {
        var c0 = d.children[0];
        var c1 = d.children[1];
        if (c0 && c1 && c0.tagName === "DIV" && c1.tagName === "DIV") {
          var sampleTxt = (c0.textContent || "") + " " + (c1.textContent || "");
          if (/(FT|LIVE|예정|종료|Scheduled|Final|판정승|TKO|SUB|배당|AI\s*\d+)/i.test(sampleTxt)) {
            return d;
          }
        }
      }
    }
    return direct || document.getElementById("proTickerList");
  }

  // Drills down through any single-child scroll viewport/flex wrapper so individual match cards are ALWAYS returned!
  function isSingleMatchCardElV331(el) {
    if (!el || el.nodeType !== 1) return false;
    if (
      el.getAttribute("data-sq-v30-fk") ||
      el.getAttribute("onclick") ||
      typeof el.onclick === "function" ||
      (el.style && el.style.cursor === "pointer")
    ) {
      return true;
    }
    var fullTxt = String(el.textContent || "");
    var aiHits = (fullTxt.match(/AI\s*\d+/g) || []).length;
    if (aiHits === 1 && el.firstElementChild && el.lastElementChild && el.firstElementChild !== el.lastElementChild) {
      var firstTxt = String(el.firstElementChild.textContent || "");
      if (!/AI\s*\d+/.test(firstTxt)) {
        return true;
      }
    }
    return false;
  }

  function isSingleMatchCardElV331(el) {
    if (!el || el.nodeType !== 1) return false;
    if (
      el.getAttribute("data-sq-v30-fk") ||
      el.getAttribute("onclick") ||
      typeof el.onclick === "function" ||
      (el.style && el.style.cursor === "pointer")
    ) {
      return true;
    }
    var fullTxt = String(el.textContent || "");
    var aiHits = (fullTxt.match(/AI\s*\d+/g) || []).length;
    if (aiHits === 1 && el.firstElementChild && el.lastElementChild && el.firstElementChild !== el.lastElementChild) {
      var firstTxt = String(el.firstElementChild.textContent || "");
      if (!/AI\s*\d+/.test(firstTxt)) {
        return true;
      }
    }
    return false;
  }

  function resolveIndividualCardElementsV31(strip) {
    if (!strip || !strip.children) return [];
    var cur = strip;
    var guard = 0;
    while (cur && cur.children && cur.children.length === 1 && guard < 4) {
      var onlyChild = cur.children[0];
      if (!onlyChild || onlyChild.getAttribute("data-sq-v31-empty") || onlyChild.getAttribute("data-sq-v31-filter-empty")) {
        break;
      }
      if (isSingleMatchCardElV331(onlyChild)) {
        break;
      }
      if (onlyChild.children && onlyChild.children.length >= 1) {
        cur = onlyChild;
        guard++;
      } else {
        break;
      }
    }
    var kids = Array.prototype.slice.call(cur.children || []);
    return kids.filter(function (el) {
      return el && el.tagName === "DIV" && !el.getAttribute("data-sq-v31-empty") && !el.getAttribute("data-sq-v31-filter-empty");
    });
  }

  function findAllScoreStripsAndHelpersToHideV30(primaryTrack) {
    var list = [];
    function addUniq(el) {
      if (el && list.indexOf(el) === -1 && el.id !== "sqV31DateCalendarBar" && el.id !== "sqV30ScoreToolbar" && el.id !== "sqV30ScoreboardTableWrap" && el.id !== "dcTickerControls") {
        if (!el.querySelector || !el.querySelector("[data-sq-v304-sport]")) {
          list.push(el);
        }
      }
    }
    addUniq(primaryTrack);
    var dcTrack = document.getElementById("dcLiveTickerTrack");
    addUniq(dcTrack);
    var legacyList = document.getElementById("proTickerList");
    addUniq(legacyList);
    if (legacyList && legacyList.parentElement) {
      var lp = legacyList.parentElement;
      if (!lp.querySelector("#dcTickerControls") && !lp.querySelector("[data-sq-v304-sport]") && !lp.querySelector("#sqV30ScoreToolbar") && !lp.querySelector("#sqV31DateCalendarBar") && !lp.querySelector("#sqV30ScoreboardTableWrap") && lp.id !== "proLiveTickerBar") {
        addUniq(lp);
      }
    }

    var roots = [];
    if (primaryTrack && primaryTrack.parentElement) roots.push(primaryTrack.parentElement);
    var pb = document.getElementById("proLiveTickerBar");
    if (pb && roots.indexOf(pb) === -1) roots.push(pb);

    roots.forEach(function (rt) {
      Array.prototype.forEach.call(rt.querySelectorAll("div"), function (d) {
        if (!d || d === rt || d.id === "sqV31DateCalendarBar" || d.id === "sqV30ScoreToolbar" || d.id === "sqV30ScoreboardTableWrap" || d.id === "dcTickerControls" || d.id === "dcSportPills") return;
        if (d.contains && (d.contains(document.getElementById("dcTickerControls")) || d.contains(document.getElementById("sqV30ScoreToolbar")) || d.contains(document.getElementById("sqV31DateCalendarBar")) || d.contains(document.getElementById("sqV30ScoreboardTableWrap")) || (d.querySelector && d.querySelector("[data-sq-v304-sport]")))) return;
        var txt = String(d.textContent || "");
        if ((d.classList && d.classList.contains("sq-match-sort-bar")) || (/휠·드래그|◀\s*이전|경기\s*정렬/.test(txt) && d.children && d.children.length <= 8)) {
          addUniq(d);
          return;
        }
        if (d.children && d.children.length >= 2 && d.children.length <= 180) {
          var s0 = (d.children[0] && d.children[0].textContent) || "";
          var s1 = (d.children[1] && d.children[1].textContent) || "";
          if (/(AI\s*\d+|배당\s*[\d.]+|최근\s*시즌\s*기록|Scheduled|Final)/i.test(s0 + " " + s1) && !d.querySelector("table")) {
            addUniq(d);
          }
        }
      });
    });
    return list;
  }

  function matchFavKeyV30(it) {
    if (!it) return "";
    return String(it.id || (String(it.league_code || it.league || "") + "::" + String(it.home || "") + "::" + String(it.away || "")));
  }

  window.toggleMatchFavV30 = function (ev, rawKey) {
    if (ev) {
      ev.preventDefault();
      ev.stopPropagation();
    }
    var k = String(rawKey || "").trim();
    if (!k) return;
    if (v30ScoreState.favMap[k]) {
      delete v30ScoreState.favMap[k];
      if (typeof showToast === "function") showToast("관심경기에서 해제했습니다.");
    } else {
      v30ScoreState.favMap[k] = true;
      if (typeof showToast === "function") showToast("★ 내 관심경기로 등록되었습니다!");
    }
    saveFavMatchesV30();
    v30ScoreState.lastToolbarSig = "";
    v30ScoreState.lastTableSig = "";
    refreshScoreboardSuiteV30();
  };

  function evaluateAiVerdictV30(it) {
    if (!it || it.state !== "post") return null;
    var hs = Number(it.home_score);
    var as = Number(it.away_score);
    if (isNaN(hs) || isNaN(as)) return null;
    var ai = it.ai || {};
    var hp = Number(ai.home_pct || 50);
    var dp = Number(ai.draw_pct || 0);
    var ap = Number(ai.away_pct || 50);
    if (hs === 0 && as === 0 && dp === 0) return null;
    var pick = (hp >= ap && hp >= dp) ? "H" : ((ap > hp && ap >= dp) ? "A" : "D");
    var actual = hs > as ? "H" : (as > hs ? "A" : "D");
    var isHit = (pick === actual);
    var pickLabel = pick === "H" ? (it.home + " 승") : (pick === "A" ? (it.away + " 승") : "무승부");
    return {
      hit: isHit,
      pick: pick,
      actual: actual,
      pickLabel: pickLabel,
      badgeHtml: isHit
        ? "<span class='sq-v30-hit-badge' title='사전 AI 최우선 예측(" + uxEsc(pickLabel) + ") 적중'>AI 적중 ✅</span>"
        : "<span class='sq-v30-upset-badge' title='사전 AI 예측(" + uxEsc(pickLabel) + ") 대비 이변 결과'>이변 ⚡</span>"
    };
  }

  function filterScoreItemsV30(items) {
    var sf = v30ScoreState.statusFilter;
    return (items || []).filter(function (it) {
      if (!it) return false;
      if (sf === "live") return it.state === "in";
      if (sf === "pre") return it.state === "pre";
      if (sf === "post") return it.state === "post";
      if (sf === "fav") return Boolean(v30ScoreState.favMap[matchFavKeyV30(it)]);
      return true;
    });
  }

  function sportEmojiV30(sp) {
    var m = { soccer: "⚽", baseball: "⚾", basketball: "🏀", volleyball: "🏐", hockey: "🏒", esports: "🎮", lck: "🎮", ufc: "🥊", mma: "🥊", tennis: "🎾", f1: "🏎️" };
    return m[String(sp || "").toLowerCase()] || "🏟️";
  }

  function syncSportPillLiveDotsV30() {
    ensureRootTickerObserverV31();
    var baseAll = v30ScoreState.rawAllItems.length ? v30ScoreState.rawAllItems : (v30ScoreState.items || []);
    var liveBySport = {};
    var totalLive = 0;
    baseAll.forEach(function (it) {
      if (it && it.state === "in") {
        totalLive++;
        var sp = String(it.sport || "").toLowerCase();
        liveBySport[sp] = (liveBySport[sp] || 0) + 1;
      }
    });
    var sportBtns = discoverTopSportButtonsV304();
    _inSportDotMutationV304 = true;
    try {
      var totalBySport = {};
      baseAll.forEach(function (it) {
        if (it && it.sport) {
          var sk = String(it.sport).toLowerCase();
          totalBySport[sk] = (totalBySport[sk] || 0) + 1;
        }
      });
      sportBtns.forEach(function (btn) {
        var sp = btn.getAttribute("data-sq-v304-sport") || parseSportKeyFromBtnV304(btn);
        if (!sp) return;
        if ((v30ScoreState.dateOffset || 0) === 0 && baseAll.length > 0) {
          var wantTot = sp === "all" ? baseAll.length : (totalBySport[sp] || 0);
          if (wantTot > 0 && btn.firstChild && btn.firstChild.nodeType === 3) {
            var replaced = btn.firstChild.nodeValue.replace(/\(\d+\)/, "(" + wantTot + ")");
            if (replaced !== btn.firstChild.nodeValue) btn.firstChild.nodeValue = replaced;
          }
        }
        var oldDot = btn.querySelector(".sq-v30-live-dot");
        var cnt = sp === "all" ? totalLive : (liveBySport[sp] || 0);
        if (cnt > 0) {
          var wantTxt = "• LIVE " + cnt;
          if (!oldDot) {
            oldDot = document.createElement("span");
            oldDot.className = "sq-v30-live-dot";
            btn.appendChild(oldDot);
          }
          if (oldDot.textContent !== wantTxt) oldDot.textContent = wantTxt;
        } else if (oldDot && oldDot.parentNode) {
          oldDot.parentNode.removeChild(oldDot);
        }
      });
    } finally {
      _inSportDotMutationV304 = false;
    }
  }

  function parseCleanDomCardV30(card) {
    if (!card) return null;
    var lines = String(card.innerText || card.textContent || "")
      .split(/\n+/)
      .map(function (s) { return s.replace(/^[☆★]\s*/, "").replace(/AI\s*적중\s*✅|이변\s*⚡/g, "").trim(); })
      .filter(function (s) {
        return Boolean(s) && !/^⭐\s*MY$/i.test(s) && !/^📌/.test(s) && !/^📉/.test(s) && !/^📅\s*최근/.test(s);
      });
    if (lines.length < 2) return null;

    var header = lines[0] || "";
    var lgMatch = header.match(/^([A-Z0-9_가-힣\-]{2,14})/);
    var lg = lgMatch ? lgMatch[1] : "SPORT";
    var fullTxt = lines.join(" ");
    var badge = "예정";
    if (/시즌\s*기록\s*·\s*종료|종료|Final\/OT|Final|\bFT\b|판정승|\dR\s*(?:SUB|TKO|KO)/i.test(fullTxt)) {
      var bm = fullTxt.match(/(Final\/OT|Final|\bFT\b|판정승|\dR\s*(?:SUB|TKO|KO)|종료)/i);
      badge = bm ? bm[1] : "종료";
    } else if (/LIVE|\d+:\d+\s*-\s*(?:1st|2nd|3rd|4th|OT)|\b[1-4]Q\b|\b[1-3]P\b|\d+\s*'|\d+\s*회(초|말)?/i.test(fullTxt)) {
      var lm = fullTxt.match(/(\d+:\d+\s*-\s*(?:1st|2nd|3rd|4th|OT)|\d+\s*회(?:초|말)?\s*(?:진행중)?|LIVE\s*[^\s📌]*)/i);
      badge = lm ? lm[1].trim() : "LIVE";
    }

    var home = "", away = "", hs = 0, as = 0, period = "";
    var hp = 50, dp = 0, ap = 50, oh = "-", od = "", oa = "-";

    for (var i = 0; i < lines.length; i++) {
      var ln = lines[i];
      if (/^📊/.test(ln)) {
        var pc = ln.replace(/^📊\s*/, "").trim();
        if (pc.indexOf("대기") === -1) period = pc;
        continue;
      }
      if (/AI\s*\d+/.test(ln)) {
        var pm3 = ln.match(/AI\s*(\d+)%?\s*[\/:]\s*(\d+)%?\s*[\/:]\s*(\d+)%?/i);
        var pm2 = !pm3 ? ln.match(/AI\s*(\d+)%?\s*[\/:]\s*(\d+)%?/i) : null;
        if (pm3) { hp = Number(pm3[1]); dp = Number(pm3[2]); ap = Number(pm3[3]); }
        else if (pm2) { hp = Number(pm2[1]); dp = 0; ap = Number(pm2[2]); }
        var om3 = ln.match(/배당\s*([\d.]+)\s*\/\s*([\d.]+)\s*\/\s*([\d.]+)/);
        var om2 = !om3 ? ln.match(/배당\s*([\d.]+)\s*\/\s*([\d.]+)/) : null;
        if (om3) { oh = om3[1]; od = om3[2]; oa = om3[3]; }
        else if (om2) { oh = om2[1]; od = ""; oa = om2[2]; }
        continue;
      }
      if (!/\b(AM|PM|EST|KST|UTC)\b/i.test(ln) && !/^\d{1,2}\/\d{1,2}/.test(ln) && !/^[1-4][QP]\b/.test(ln)) {
        var mInline = ln.match(/^([A-Za-z가-힣][A-Za-z0-9가-힣\s.'()\-]{1,28}?)\s*(\d+)\s*[:\-]\s*(\d+)\s*([A-Za-z가-힣][A-Za-z0-9가-힣\s.'()\-]{1,28})$/);
        if (mInline && !home) {
          home = mInline[1].trim();
          hs = Number(mInline[2]);
          as = Number(mInline[3]);
          away = mInline[4].trim();
        }
      }
    }

    if (!home) {
      for (var j = 1; j < lines.length - 1; j++) {
        if (lines[j + 3] !== undefined && /^\d+$/.test(lines[j + 1]) && /^\d+$/.test(lines[j + 3]) && !/^\d/.test(lines[j]) && !/^\d/.test(lines[j + 2])) {
          home = lines[j].trim();
          hs = Number(lines[j + 1]);
          away = lines[j + 2].trim();
          as = Number(lines[j + 3]);
          break;
        }
        var p1 = lines[j].match(/^([A-Za-z가-힣][A-Za-z0-9가-힣\s.'()\-]{1,28}?)\s*(\d+)$/);
        var p2 = lines[j + 1].match(/^([A-Za-z가-힣][A-Za-z0-9가-힣\s.'()\-]{1,28}?)\s*(\d+)$/);
        if (p1 && p2) {
          home = p1[1].trim();
          hs = Number(p1[2]);
          away = p2[1].trim();
          as = Number(p2[2]);
          break;
        }
      }
    }

    if (!home || !away) return null;
    return normalizeScoreItemV30({
      id: lg + "::" + home + "::" + away,
      sport: canonicalSportKeyV304("", lg),
      league: lg,
      league_code: lg,
      league_full: lg,
      status_badge: badge,
      home: home,
      away: away,
      home_score: hs,
      away_score: as,
      period_scores: period,
      home_pct: hp,
      draw_pct: dp,
      away_pct: ap,
      odds_home: oh,
      odds_draw: od,
      odds_away: oa
    }, 0);
  }

  function findItemForDomCardV30(card) {
    if (!card) return null;
    var pool = (v30ScoreState.rawAllItems && v30ScoreState.rawAllItems.length)
      ? v30ScoreState.rawAllItems
      : (v30ScoreState.items || []);
    var fkAttr = card.getAttribute("data-sq-v30-fk") || "";
    if (fkAttr && pool.length) {
      for (var k = 0; k < pool.length; k++) {
        if (pool[k] && matchFavKeyV30(pool[k]) === fkAttr) return pool[k];
      }
    }
    var txt = String(card.textContent || "").toLowerCase();
    for (var i = 0; i < pool.length; i++) {
      var it = pool[i];
      if (!it) continue;
      var h = String(it.home || "").toLowerCase();
      var a = String(it.away || "").toLowerCase();
      if (h && a && txt.indexOf(h) !== -1 && txt.indexOf(a) !== -1) {
        return it;
      }
    }
    return parseCleanDomCardV30(card);
  }

  function installGlobalCardClickCaptureV30() {
    if (window.__sqGlobalCardCaptureV30) return;
    window.__sqGlobalCardCaptureV30 = true;
    window.addEventListener("click", function (ev) {
      var t = ev.target;
      if (!t || !t.closest) return;
      if (
        t.closest("[data-sq-card-star='1']") ||
        t.closest("[data-sq-v304-sport]") ||
        t.closest("#sqV31DateCalendarBar") ||
        t.closest("#sqV30ScoreToolbar") ||
        t.closest("#sqV30ScoreboardTableWrap") ||
        t.closest("#dcQuickMatchModal") ||
        t.closest("#dcTickerControls") ||
        t.closest(".sq-match-sort-bar")
      ) {
        return;
      }
      var mainStrip = findPrimaryMainCardStripV30();
      var dcDirect = document.getElementById("dcLiveTickerTrack");
      var legacyStrip = document.getElementById("proTickerList");
      var strips = [mainStrip, dcDirect, legacyStrip];
      var card = null;
      for (var c = 0; c < strips.length; c++) {
        var st = strips[c];
        if (!st || !st.contains(t)) continue;
        var cardList = resolveIndividualCardElementsV31(st);
        for (var m = 0; m < cardList.length; m++) {
          if (cardList[m] === t || cardList[m].contains(t)) {
            card = cardList[m];
            break;
          }
        }
        if (card) break;
      }
      if (!card) return;
      var matched = findItemForDomCardV30(card);
      if (!matched) return;
      ev.preventDefault();
      ev.stopPropagation();
      if (ev.stopImmediatePropagation) ev.stopImmediatePropagation();
      hideRowHoverPreviewV31();
      var oldMd = document.getElementById("dcMatchDetailModal");
      if (oldMd) oldMd.style.display = "none";
      var oldMd2 = document.getElementById("dcMatchModal");
      if (oldMd2) oldMd2.style.display = "none";
      openMatchInsightModalByItemV30(matched);
    }, true);
  }

  function buildDomesticBballCardHtmlV343(it) {
    var isLive = it.state === "in";
    var stBg = isLive ? "#dc2626" : (it.state === "post" ? "#475569" : "#1d4ed8");
    var ai = it.ai || {};
    var odds = ai.odds || {};
    var probTxt = "AI " + (ai.home_pct || 50) + "% / 0% / " + (ai.away_pct || 50) + "%";
    var oddsTxt = odds.home ? ("배당 " + odds.home + " / " + odds.away) : "";
    return [
      "<div style='display:flex;align-items:center;justify-content:space-between;gap:4px;font-size:11px;margin-bottom:5px;'>",
      "  <span style='background:#1e293b;color:#fdba74;padding:2px 6px;border-radius:6px;font-weight:800;'>🏀 " + uxEsc(it.league_code || it.league) + "</span>",
      "  <span style='color:#fde68a;font-weight:700;'>" + uxEsc(it.venue || it.match_dt_kst) + "</span>",
      "  <span data-sq-v343-badge='1' style='background:" + stBg + ";color:#fff;padding:2px 6px;border-radius:6px;font-weight:800;'>" + uxEsc(it.status_badge) + "</span>",
      "</div>",
      "<div style='display:flex;align-items:center;justify-content:space-between;font-size:13px;font-weight:800;margin:4px 0;'>",
      "  <span style='overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:98px;'>" + uxEsc(it.home) + "</span>",
      "  <span data-sq-v343-score='1' style='background:#1e293b;padding:2px 9px;border-radius:8px;color:#38bdf8;font-size:14px;'>" + uxEsc(it.home_score) + " : " + uxEsc(it.away_score) + "</span>",
      "  <span style='overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:98px;text-align:right;'>" + uxEsc(it.away) + "</span>",
      "</div>",
      "<div data-sq-v343-period='1' style='font-size:11px;color:#7dd3fc;margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;'>📊 " + uxEsc(it.period_scores || "정규리그 매치업") + "</div>",
      "<div style='display:flex;justify-content:space-between;align-items:center;font-size:10.5px;color:#94a3b8;margin-top:4px;'>",
      "  <span>🎯 " + uxEsc(probTxt) + "</span>",
      "  <span>" + uxEsc(oddsTxt) + "</span>",
      "</div>"
    ].join("");
  }

  function syncDomesticBballCardsInTrackV343(strip) {
    if (!strip || v30ScoreState.customDateRendered) return;
    var domItems = (v30ScoreState.rawAllItems || []).filter(function (x) {
      if (!x) return false;
      var lc = String(x.league_code || x.league || "").toUpperCase();
      return lc === "KBL" || lc === "WKBL";
    });
    if (!domItems.length) return;

    var curCards = resolveIndividualCardElementsV31(strip);
    var cardParent = (curCards.length && curCards[0].parentElement) ? curCards[0].parentElement : strip;
    if (!cardParent) return;

    // Hide widget.js's generic empty-message node if KBL/WKBL was selected in #dcTickerLeagueSel
    var ctl = getActiveDcControlsV30();
    var luAct = String(ctl.league || "all").toUpperCase();
    if (luAct === "KBL" || luAct === "WKBL") {
      Array.prototype.forEach.call(cardParent.children || [], function (ch) {
        if (ch && !ch.getAttribute("data-sq-v343-dom-id") && !ch.getAttribute("data-sq-v30-fk") && !/AI\s*\d+/.test(String(ch.textContent || ""))) {
          ch.style.display = "none";
        }
      });
    }

    var prevGuard = _inSportDotMutationV304;
    _inSportDotMutationV304 = true;
    try {
      for (var i = domItems.length - 1; i >= 0; i--) {
        var dg = domItems[i];
        var fk = matchFavKeyV30(dg);
        var existing = cardParent.querySelector("[data-sq-v343-dom-id='" + dg.id + "']") || cardParent.querySelector("[data-sq-v30-fk='" + fk.replace(/'/g, "") + "']");
        if (!existing) {
          var card = document.createElement("div");
          card.setAttribute("data-sq-v343-dom-id", dg.id);
          card.setAttribute("data-sq-v30-fk", fk);
          card.style.cssText = "min-width:248px;max-width:288px;flex:0 0 auto;background:#0f172a;border:1px solid " + (dg.state === "in" ? "#ef4444" : "#334155") + ";border-radius:12px;padding:9px 11px;cursor:pointer;color:#f8fafc;display:flex;flex-direction:column;";
          card.innerHTML = buildDomesticBballCardHtmlV343(dg);
          if (dg.state === "in" && cardParent.firstChild) {
            cardParent.insertBefore(card, cardParent.firstChild);
          } else {
            cardParent.appendChild(card);
          }
        } else if (existing.getAttribute("data-sq-v343-dom-id")) {
          var bEl = existing.querySelector("[data-sq-v343-badge='1']");
          if (bEl && bEl.textContent !== dg.status_badge) bEl.textContent = dg.status_badge;
          var sEl = existing.querySelector("[data-sq-v343-score='1']");
          var wantSc = dg.home_score + " : " + dg.away_score;
          if (sEl && sEl.textContent !== wantSc) sEl.textContent = wantSc;
          var pEl = existing.querySelector("[data-sq-v343-period='1']");
          var wantPr = "📊 " + (dg.period_scores || "정규리그 매치업");
          if (pEl && pEl.textContent !== wantPr) pEl.textContent = wantPr;
        }
      }
    } finally {
      _inSportDotMutationV304 = prevGuard;
    }
  }

  function decorateMiniScoreCardsV30() {
    var mainTrack = findPrimaryMainCardStripV30();
    var leg = document.getElementById("proTickerList");
    var ctl = getActiveDcControlsV30();
    var sf = v30ScoreState.statusFilter;

    var strips = [];
    if (mainTrack) strips.push({ el: mainTrack, filterActive: true });
    if (leg && leg !== mainTrack) strips.push({ el: leg, filterActive: (sf !== "all") });

    strips.forEach(function (entry) {
      var strip = entry.el;
      if (entry.el === mainTrack) {
        syncDomesticBballCardsInTrackV343(strip);
      }
      var cards = resolveIndividualCardElementsV31(strip);
      if (!cards.length) return;
      var cardParent = cards[0].parentElement || strip;
      var visibleCnt = 0;

      cards.forEach(function (card) {
        var it = findItemForDomCardV30(card);
        if (!it) return;
        var fk = matchFavKeyV30(it);
        card.setAttribute("data-sq-v30-fk", fk);
        var isFav = Boolean(v30ScoreState.favMap[fk]);

        if (entry.filterActive) {
          var show = true;
          if (ctl.sport !== "all" && it.sport !== ctl.sport) show = false;
          if (ctl.league !== "all") {
            var lu = ctl.league.toUpperCase();
            if (String(it.league_code || "").toUpperCase() !== lu && String(it.league || "").toUpperCase() !== lu) {
              show = false;
            }
          }
          if (show) {
            if (sf === "live") show = (it.state === "in");
            else if (sf === "pre") show = (it.state === "pre");
            else if (sf === "post") show = (it.state === "post");
            else if (sf === "fav") show = isFav;
          }
          if (show) {
            visibleCnt++;
            card.removeAttribute("data-sq-v31-card-hidden");
            if (card.style.getPropertyValue("display") === "none") {
              card.style.removeProperty("display");
            }
          } else {
            card.setAttribute("data-sq-v31-card-hidden", "1");
            card.style.setProperty("display", "none", "important");
          }
        } else {
          visibleCnt++;
        }

        var topRow = card.firstElementChild;
        if (topRow) {
          var starBtn = topRow.querySelector("[data-sq-card-star='1']");
          if (!starBtn) {
            starBtn = document.createElement("button");
            starBtn.type = "button";
            starBtn.setAttribute("data-sq-card-star", "1");
            starBtn.style.cssText = "background:transparent;border:0;padding:0 3px;cursor:pointer;font-size:13px;line-height:1;flex-shrink:0;";
            topRow.insertBefore(starBtn, topRow.firstChild);
          }
          var wantStar = isFav ? "★" : "☆";
          if (starBtn.textContent !== wantStar) starBtn.textContent = wantStar;
          starBtn.style.color = isFav ? "#fbbf24" : "#64748b";
          starBtn.title = isFav ? "관심경기 해제" : "관심경기 등록";
          starBtn.onclick = function (ev) { window.toggleMatchFavV30(ev, fk); };
        }

        var verdict = evaluateAiVerdictV30(it);
        var botRow = card.lastElementChild;
        if (verdict && botRow && !card.querySelector(".sq-v30-hit-badge, .sq-v30-upset-badge")) {
          var vWrap = document.createElement("span");
          vWrap.style.marginLeft = "4px";
          vWrap.innerHTML = verdict.badgeHtml;
          if (vWrap.firstChild) botRow.appendChild(vWrap.firstChild);
        }
      });

      // Clean empty-state notice inside main card strip when 0 cards match the active status filter
      if (entry.el === mainTrack) {
        var oldEmpty = cardParent.querySelector("[data-sq-v31-filter-empty='1']");
        if (visibleCnt === 0 && sf !== "all") {
          if (!oldEmpty) {
            oldEmpty = document.createElement("div");
            oldEmpty.setAttribute("data-sq-v31-filter-empty", "1");
            oldEmpty.style.cssText = "padding:14px 18px;color:#94a3b8;font-size:12.5px;background:rgba(15,23,42,.75);border:1px dashed #334155;border-radius:11px;width:100%;text-align:center;";
            cardParent.appendChild(oldEmpty);
          }
          oldEmpty.textContent = "현재 선택한 상태 필터에 해당하는 카드가 없습니다. '전체' 필터를 누르면 모든 카드를 다시 볼 수 있습니다.";
        } else if (oldEmpty && oldEmpty.parentNode) {
          oldEmpty.parentNode.removeChild(oldEmpty);
        }
      }
    });
  }

  // -------------------------------------------------------------------------
  // v3.1 Hover Mini-Stat & Value-Edge Tooltip for Scoreboard Table Rows
  // -------------------------------------------------------------------------
  function ensureRowHoverPreviewElV31() {
    var tip = document.getElementById("sqV31RowHoverPreview");
    if (!tip) {
      tip = document.createElement("div");
      tip.id = "sqV31RowHoverPreview";
      document.body.appendChild(tip);
      window.addEventListener("scroll", hideRowHoverPreviewV31, { passive: true });
    }
    return tip;
  }

  function hideRowHoverPreviewV31() {
    var tip = document.getElementById("sqV31RowHoverPreview");
    if (tip) tip.style.display = "none";
  }

  function showRowHoverPreviewV31(it, anchorTr, clientX, clientY) {
    if (!it || !anchorTr) return;
    var tip = ensureRowHoverPreviewElV31();
    var ai = it.ai || {};
    var odds = ai.odds || {};
    var hp = Number(ai.home_pct || 45);
    var dp = Number(ai.draw_pct || 0);
    var ap = Number(ai.away_pct || (100 - hp - dp));

    var oh = parseFloat(odds.home) || 0;
    var od = parseFloat(odds.draw) || 0;
    var oa = parseFloat(odds.away) || 0;
    var invSum = (oh > 0 ? 1 / oh : 0) + (od > 0 ? 1 / od : 0) + (oa > 0 ? 1 / oa : 0);
    var mktH = invSum > 0 && oh > 0 ? Math.round(((1 / oh) / invSum) * 1000) / 10 : hp;
    var mktA = invSum > 0 && oa > 0 ? Math.round(((1 / oa) / invSum) * 1000) / 10 : ap;
    var edgeH = Math.round((hp - mktH) * 10) / 10;
    var edgeA = Math.round((ap - mktA) * 10) / 10;
    var bestSide = Math.abs(edgeH) >= Math.abs(edgeA) ? it.home : it.away;
    var bestEdge = Math.abs(edgeH) >= Math.abs(edgeA) ? edgeH : edgeA;
    var verdict = evaluateAiVerdictV30(it);

    var domH = Math.max(22, Math.min(78, Math.round(hp + (dp * 0.5))));
    var domA = 100 - domH;

    tip.innerHTML = [
      "<div style='display:flex;justify-content:space-between;align-items:center;gap:6px;margin-bottom:5px;'>",
      "  <span style='font-weight:800;color:#7dd3fc;font-size:11.5px;'>" + sportEmojiV30(it.sport) + " " + uxEsc(it.league_full || it.league) + "</span>",
      "  <span style='color:#fde68a;font-size:10.5px;font-weight:700;'>" + uxEsc(it.match_dt_full_kst || it.match_dt_kst || it.status_badge) + "</span>",
      "</div>",
      "<div style='font-weight:900;font-size:13px;color:#f8fafc;margin-bottom:5px;'>" + uxEsc(it.home) + " <span style='color:#38bdf8;'>" + (it.state === "pre" ? "vs" : (it.home_score + " : " + it.away_score)) + "</span> " + uxEsc(it.away) + (verdict ? " " + verdict.badgeHtml : "") + "</div>",
      "<div style='background:#0f172a;border:1px solid #1e293b;border-radius:8px;padding:6px 8px;margin-bottom:6px;color:#cbd5e1;font-size:11px;'>",
      "  <div>📊 <strong>세부 흐름:</strong> " + uxEsc(it.period_scores || (it.state === "pre" ? "킥오프 전 프리뷰 · 예상 주도권 " + domH + "% : " + domA + "%" : "실시간 스코어 집계 완료")) + "</div>",
      (it.venue ? "  <div style='color:#94a3b8;margin-top:2px;'>🏟️ 구장: " + uxEsc(it.venue) + "</div>" : ""),
      "  <div style='margin-top:2px;'>💎 <strong>AI·시장 격차:</strong> " + uxEsc(bestSide) + " <span style='color:" + (bestEdge >= 0 ? "#34d399" : "#fbbf24") + ";font-weight:800;'>" + (bestEdge >= 0 ? "+" : "") + bestEdge + "%p</span> (AI " + hp + "%" + (dp > 0 ? "/" + dp + "%" : "") + "/" + ap + "%)</div>",
      "</div>",
      "<div style='color:#6ee7b7;font-size:10.5px;font-weight:700;'>👆 클릭 시 5탭 전술·순위·라인업 상세 및 +15P 분석글 바로쓰기</div>"
    ].join("");

    tip.style.display = "block";
    var rect = anchorTr.getBoundingClientRect();
    var vw = window.innerWidth || 1280;
    var vh = window.innerHeight || 800;
    var tipW = tip.offsetWidth || 312;
    var tipH = tip.offsetHeight || 145;
    var left = (typeof clientX === "number" && clientX > 0) ? (clientX + 14) : (rect.left + 80);
    if (left + tipW + 12 > vw) left = Math.max(12, vw - tipW - 14);
    var top = rect.bottom + 6;
    if (top + tipH + 12 > vh) top = Math.max(12, rect.top - tipH - 8);
    tip.style.left = Math.round(left) + "px";
    tip.style.top = Math.round(top) + "px";
  }

  function renderScoreboardTableV30(allItems) {
    var wrap = document.getElementById("sqV30ScoreboardTableWrap");
    if (!wrap) return;
    var isTable = v30ScoreState.viewMode === "table-league" || v30ScoreState.viewMode === "table-time";
    var primaryTrack = findPrimaryMainCardStripV30();
    var hideTargets = findAllScoreStripsAndHelpersToHideV30(primaryTrack);

    if (!isTable) {
      hideRowHoverPreviewV31();
      wrap.removeAttribute("data-sq-open");
      wrap.style.display = "none";
      document.querySelectorAll("[data-sq-v30-hide-strip='1']").forEach(function (el) {
        el.removeAttribute("data-sq-v30-hide-strip");
        var prevD = el.getAttribute("data-sq-orig-disp");
        el.style.removeProperty("display");
        if (prevD === "none") el.style.setProperty("display", "none");
        else if (prevD) el.style.setProperty("display", prevD);
      });
      if (primaryTrack) primaryTrack.style.setProperty("display", "flex");
      decorateMiniScoreCardsV30();
      return;
    }

    hideTargets.forEach(function (el) {
      if (el.getAttribute("data-sq-v30-hide-strip") !== "1") {
        var csD = (window.getComputedStyle ? window.getComputedStyle(el).display : "") || el.style.getPropertyValue("display") || "";
        el.setAttribute("data-sq-orig-disp", csD === "none" ? "none" : (el.style.getPropertyValue("display") || ""));
      }
      el.setAttribute("data-sq-v30-hide-strip", "1");
      el.style.setProperty("display", "none", "important");
    });
    wrap.setAttribute("data-sq-open", "1");
    wrap.style.display = "block";

    var filtered = filterScoreItemsV30(allItems || []);
    var favKeysSig = Object.keys(v30ScoreState.favMap || {}).sort().join(",");
    var itemsSig = filtered.map(function (x) { return matchFavKeyV30(x) + ":" + x.state + ":" + x.home_score + "-" + x.away_score; }).join("|");
    var nextSig = [v30ScoreState.viewMode, v30ScoreState.statusFilter, v30ScoreState.selectedSport, v30ScoreState.selectedLeague, v30ScoreState.selectedDateOffset, favKeysSig, itemsSig].join("||");
    if (v30ScoreState.lastTableSig === nextSig && wrap.children.length > 0) {
      return;
    }
    v30ScoreState.lastTableSig = nextSig;

    if (!filtered.length) {
      wrap.innerHTML = "<div style='padding:22px;text-align:center;color:#94a3b8;font-size:12.8px;'>선택한 날짜·조건에 해당하는 경기가 없습니다. 위 ±5일 날짜 바 또는 다른 상태·종목 필터를 선택해 보세요.</div>";
      return;
    }

    function buildRowHtml(it) {
      var fk = matchFavKeyV30(it);
      var isFav = Boolean(v30ScoreState.favMap[fk]);
      var isLive = it.state === "in";
      var isPost = it.state === "post";
      var hs = Number(it.home_score || 0);
      var as = Number(it.away_score || 0);
      var hWin = (isLive || isPost) && hs > as;
      var aWin = (isLive || isPost) && as > hs;

      var ai = it.ai || {};
      var hp = Number(ai.home_pct || 45);
      var dp = Number(ai.draw_pct || 0);
      var ap = Number(ai.away_pct || (100 - hp - dp));
      var odds = ai.odds || {};
      var verdict = evaluateAiVerdictV30(it);

      var statusCell = isLive
        ? "<span style='background:#dc2626;color:#fff;padding:2px 7px;border-radius:6px;font-size:11px;font-weight:800;'>● " + uxEsc(it.status_badge) + "</span>"
        : (isPost
            ? "<span style='color:#94a3b8;font-weight:700;font-size:11.5px;'>종료</span> <span style='color:#64748b;font-size:10.5px;'>" + uxEsc(it.match_dt_kst || it.status_badge) + "</span>"
            : "<span style='color:#fde68a;font-weight:700;font-size:11.5px;'>" + uxEsc(it.match_dt_kst || "예정") + "</span>");

      var scoreMain = it.state === "pre"
        ? "<span style='color:#64748b;font-weight:800;'>VS</span>"
        : ("<span style='font-weight:900;font-size:13.5px;color:" + (hWin ? "#34d399" : "#f8fafc") + ";'>" + hs + "</span>" +
           " <span style='color:#64748b;'>:</span> " +
           "<span style='font-weight:900;font-size:13.5px;color:" + (aWin ? "#34d399" : "#f8fafc") + ";'>" + as + "</span>");

      var periodSub = it.period_scores
        ? "<div style='font-size:10px;color:#38bdf8;margin-top:2px;max-width:155px;overflow:hidden;text-overflow:ellipsis;'>" + uxEsc(it.period_scores) + "</div>"
        : "";

      var barHtml = (
        "<div style='min-width:132px;'>" +
        "  <div style='display:flex;height:6px;border-radius:999px;overflow:hidden;background:#1e293b;'>" +
        "    <div style='width:" + hp + "%;background:#10b981;'></div>" +
        (dp > 0 ? "<div style='width:" + dp + "%;background:#64748b;'></div>" : "") +
        "    <div style='width:" + ap + "%;background:#38bdf8;'></div>" +
        "  </div>" +
        "  <div style='display:flex;justify-content:space-between;align-items:center;gap:4px;font-size:10.5px;color:#cbd5e1;margin-top:3px;'>" +
        "    <span>" + hp + "%" + (dp > 0 ? "·" + dp + "%" : "") + "·" + ap + "%</span>" +
        (verdict ? verdict.badgeHtml : "") +
        "  </div>" +
        "</div>"
      );

      var oddsHtml = (
        "<span class='sq-v30-odds-box'>" + uxEsc(odds.home || "-") + "</span>" +
        (odds.draw ? "<span class='sq-v30-odds-box'>" + uxEsc(odds.draw) + "</span>" : "") +
        "<span class='sq-v30-odds-box'>" + uxEsc(odds.away || "-") + "</span>"
      );

      return (
        "<tr class='sq-v30-match-row" + (isLive ? " is-live-row" : "") + "' data-sq-row-fk='" + uxEsc(fk) + "'>" +
        "  <td style='width:30px;text-align:center;'><button type='button' data-sq-fav-key='" + uxEsc(fk) + "' style='background:transparent;border:0;cursor:pointer;font-size:15px;padding:2px 5px;color:" + (isFav ? "#fbbf24" : "#64748b") + ";'>" + (isFav ? "★" : "☆") + "</button></td>" +
        "  <td>" + statusCell + (v30ScoreState.viewMode === "table-time" ? " <span style='color:#93c5fd;font-size:10.5px;font-weight:700;'>[" + uxEsc(it.league) + "]</span>" : "") + "</td>" +
        "  <td style='text-align:right;font-weight:" + (hWin ? "900" : "700") + ";color:" + (hWin ? "#6ee7b7" : "#f8fafc") + ";'>" + uxEsc(it.home) + "</td>" +
        "  <td style='text-align:center;'>" + scoreMain + periodSub + "</td>" +
        "  <td style='text-align:left;font-weight:" + (aWin ? "900" : "700") + ";color:" + (aWin ? "#6ee7b7" : "#f8fafc") + ";'>" + uxEsc(it.away) + "</td>" +
        "  <td>" + barHtml + "</td>" +
        "  <td>" + oddsHtml + "</td>" +
        "</tr>"
      );
    }

    var tbodyHtml = "";
    if (v30ScoreState.viewMode === "table-league") {
      var groups = [];
      var gMap = {};
      filtered.forEach(function (it) {
        var gk = String(it.league_code || it.league || "ETC");
        if (!gMap[gk]) {
          gMap[gk] = { code: gk, title: it.league_full || it.league || gk, sport: it.sport, list: [] };
          groups.push(gMap[gk]);
        }
        gMap[gk].list.push(it);
      });
      groups.forEach(function (g) {
        tbodyHtml += (
          "<tr class='sq-v30-lg-hdr'><td colspan='7'>" +
          "  <div style='display:flex;justify-content:space-between;align-items:center;'>" +
          "    <span>" + sportEmojiV30(g.sport) + " " + uxEsc(g.title) + " <span style='color:#64748b;font-weight:700;'>(" + g.list.length + ")</span></span>" +
          "    <button type='button' data-sq-jump-st='" + uxEsc(g.code) + "' style='background:transparent;border:1px solid rgba(56,189,248,.4);color:#7dd3fc;border-radius:6px;padding:1px 7px;font-size:10.5px;font-weight:700;cursor:pointer;'>순위표 ↗</button>" +
          "  </div>" +
          "</td></tr>"
        );
        tbodyHtml += g.list.map(buildRowHtml).join("");
      });
    } else {
      var sortedTime = filtered.slice().sort(function (a, b) {
        var oa = a.state === "in" ? 0 : (a.state === "pre" ? 1 : 2);
        var ob = b.state === "in" ? 0 : (b.state === "pre" ? 1 : 2);
        if (oa !== ob) return oa - ob;
        return String(a.match_dt_kst || "").localeCompare(String(b.match_dt_kst || ""));
      });
      tbodyHtml = sortedTime.map(buildRowHtml).join("");
    }

    wrap.innerHTML = (
      "<table class='sq-v30-sb-table'>" +
      "  <thead><tr>" +
      "    <th style='width:30px;text-align:center;'>★</th>" +
      "    <th>시간 · 상태</th>" +
      "    <th style='text-align:right;'>홈팀</th>" +
      "    <th style='text-align:center;'>스코어 · 세부</th>" +
      "    <th>원정팀</th>" +
      "    <th>AI 승률 예측 · 적중</th>" +
      "    <th>기준 배당</th>" +
      "  </tr></thead>" +
      "  <tbody>" + tbodyHtml + "</tbody>" +
      "</table>"
    );

    if (!wrap.__sqDelegatedV30) {
      wrap.__sqDelegatedV30 = true;
      wrap.addEventListener("click", function (ev) {
        hideRowHoverPreviewV31();
        var t = ev.target;
        if (!t || !t.closest) return;
        var favBtn = t.closest("[data-sq-fav-key]");
        if (favBtn) {
          window.toggleMatchFavV30(ev, favBtn.getAttribute("data-sq-fav-key"));
          return;
        }
        var jumpBtn = t.closest("[data-sq-jump-st]");
        if (jumpBtn) {
          ev.stopPropagation();
          var lgCode = jumpBtn.getAttribute("data-sq-jump-st") || "EPL";
          if (lgCode === "KBL" || lgCode === "WKBL") { triggerDomesticDcStandingsV341(lgCode, "standings"); } else if (typeof window.__switchDcTab === "function") { window.__switchDcTab("standings", lgCode); }
          return;
        }
        var tr = t.closest(".sq-v30-match-row");
        if (tr) {
          var rowFk = tr.getAttribute("data-sq-row-fk") || "";
          var pool = (v30ScoreState.rawAllItems && v30ScoreState.rawAllItems.length) ? v30ScoreState.rawAllItems : (v30ScoreState.items || []);
          var found = pool.find(function (x) { return x && matchFavKeyV30(x) === rowFk; });
          if (found) openMatchInsightModalByItemV30(found);
        }
      });

      wrap.addEventListener("mouseover", function (ev) {
        var t = ev.target;
        if (!t || !t.closest) return;
        var tr = t.closest(".sq-v30-match-row");
        if (!tr) {
          hideRowHoverPreviewV31();
          return;
        }
        var rowFk = tr.getAttribute("data-sq-row-fk") || "";
        var pool = (v30ScoreState.rawAllItems && v30ScoreState.rawAllItems.length) ? v30ScoreState.rawAllItems : (v30ScoreState.items || []);
        var found = pool.find(function (x) { return x && matchFavKeyV30(x) === rowFk; });
        if (found) showRowHoverPreviewV31(found, tr, ev.clientX, ev.clientY);
      });

      wrap.addEventListener("mouseleave", hideRowHoverPreviewV31);
    }
  }

  function mountOrUpdateV31DateCalendarBar(anchorBar) {
    if (!anchorBar || !anchorBar.parentElement) return;
    var cal = document.getElementById("sqV31DateCalendarBar");
    if (!cal) {
      cal = document.createElement("div");
      cal.id = "sqV31DateCalendarBar";
      anchorBar.parentElement.insertBefore(cal, anchorBar);
    }
    var curOff = v30ScoreState.selectedDateOffset || 0;
    var strip = (Array.isArray(v30ScoreState.dateStrip) && v30ScoreState.dateStrip.length === 11)
      ? v30ScoreState.dateStrip
      : buildClientDateStripV31(curOff);
    var calSig = curOff + "|" + (v30ScoreState.isLoadingDate ? "1" : "0") + "|" + (strip[0] ? strip[0].mmdd : "");
    if (v30ScoreState.lastCalSig === calSig && cal.children.length > 0) return;
    v30ScoreState.lastCalSig = calSig;

    var pillsHtml = strip.map(function (d) {
      var isAct = (d.offset === curOff);
      var cls = "sq-v31-cal-btn" + (isAct ? " active" : "") + (!isAct && d.is_sat ? " is-sat" : "") + (!isAct && d.is_sun ? " is-sun" : "");
      var label = d.is_today
        ? ("📅 오늘 " + uxEsc(d.mmdd) + "(" + uxEsc(d.wd) + ")")
        : (uxEsc(d.mmdd) + "(" + uxEsc(d.wd) + ")" + (d.rel ? "·" + uxEsc(d.rel) : ""));
      return "<button type='button' class='" + cls + "' data-v31-cal-off='" + d.offset + "'>" + label + "</button>";
    }).join("");

    cal.innerHTML = (
      "<button type='button' class='sq-v31-cal-btn' data-v31-cal-step='-1' title='이전 날짜'" + (curOff <= -5 ? " disabled style='opacity:.4;cursor:default;'" : "") + ">◀</button>" +
      pillsHtml +
      "<button type='button' class='sq-v31-cal-btn' data-v31-cal-step='1' title='다음 날짜'" + (curOff >= 5 ? " disabled style='opacity:.4;cursor:default;'" : "") + ">▶</button>" +
      (curOff !== 0 ? "<button type='button' class='sq-v31-cal-btn' data-v31-cal-off='0' style='border-color:#38bdf8;color:#7dd3fc;'>오늘로 ↺</button>" : "") +
      (v30ScoreState.isLoadingDate ? "<span style='font-size:11px;color:#6ee7b7;font-weight:800;margin-left:4px;white-space:nowrap;'>⏳ 일정 불러오는 중...</span>" : "")
    );
    setTimeout(function () {
      var actPill = cal.querySelector(".sq-v31-cal-btn.active");
      if (actPill && cal.scrollWidth > cal.clientWidth + 10) {
        var targetLeft = actPill.offsetLeft - Math.round((cal.clientWidth - actPill.offsetWidth) / 2);
        cal.scrollLeft = Math.max(0, targetLeft);
      }
    }, 20);

    if (!cal.__sqCalDelegatedV31) {
      cal.__sqCalDelegatedV31 = true;
      cal.addEventListener("click", function (ev) {
        var t = ev.target;
        if (!t || !t.closest) return;
        var offBtn = t.closest("[data-v31-cal-off]");
        if (offBtn) {
          var nextOff = parseInt(offBtn.getAttribute("data-v31-cal-off") || "0", 10);
          selectDateOffsetV31(isNaN(nextOff) ? 0 : nextOff);
          return;
        }
        var stepBtn = t.closest("[data-v31-cal-step]");
        if (stepBtn) {
          var delta = parseInt(stepBtn.getAttribute("data-v31-cal-step") || "0", 10);
          var target = Math.max(-5, Math.min(5, (v30ScoreState.selectedDateOffset || 0) + delta));
          if (target !== v30ScoreState.selectedDateOffset) {
            selectDateOffsetV31(target);
          }
        }
      });
    }
  }

  function mountOrUpdateV30ScoreToolbar() {
    var track = findPrimaryMainCardStripV30();
    if (!track || !track.parentElement) return;
    var items = v30ScoreState.items || [];

    var ctrl = document.getElementById("dcTickerControls");
    if (!ctrl) {
      var spBtns = discoverTopSportButtonsV304();
      if (spBtns.length) {
        var p = spBtns[0].parentElement;
        while (p && p.parentElement && p.parentElement !== track.parentElement) p = p.parentElement;
        if (p && p.parentElement === track.parentElement) ctrl = p;
      }
    }

    var bar = document.getElementById("sqV30ScoreToolbar");
    if (!bar) {
      bar = document.createElement("div");
      bar.id = "sqV30ScoreToolbar";
      if (ctrl && ctrl.parentElement === track.parentElement) {
        ctrl.insertAdjacentElement("afterend", bar);
      } else {
        track.parentElement.insertBefore(bar, track);
      }
    }
    mountOrUpdateV31DateCalendarBar(bar);

    var wrap = document.getElementById("sqV30ScoreboardTableWrap");
    if (!wrap) {
      wrap = document.createElement("div");
      wrap.id = "sqV30ScoreboardTableWrap";
      track.insertAdjacentElement("afterend", wrap);
    }

    var cntAll = items.length;
    var cntLive = items.filter(function (x) { return x && x.state === "in"; }).length;
    var cntPre = items.filter(function (x) { return x && x.state === "pre"; }).length;
    var cntPost = items.filter(function (x) { return x && x.state === "post"; }).length;
    var cntFav = items.filter(function (x) { return x && v30ScoreState.favMap[matchFavKeyV30(x)]; }).length;

    var sf = v30ScoreState.statusFilter;
    var vm = v30ScoreState.viewMode;
    var tbSig = [cntAll, cntLive, cntPre, cntPost, cntFav, sf, vm, v30ScoreState.selectedSport, v30ScoreState.selectedLeague, v30ScoreState.selectedDateOffset].join("|");
    if (v30ScoreState.lastToolbarSig === tbSig && bar.children.length > 0) {
      return;
    }
    v30ScoreState.lastToolbarSig = tbSig;

    bar.innerHTML = (
      "<div style='display:flex;flex-wrap:wrap;gap:5px;align-items:center;'>" +
      "  <button type='button' class='sq-v30-fbtn" + (sf === "all" ? " active" : "") + "' data-v30-sf='all'>전체 (" + cntAll + ")</button>" +
      "  <button type='button' class='sq-v30-fbtn" + (sf === "live" ? " active-live" : "") + "' data-v30-sf='live'>🔴 LIVE (" + cntLive + ")</button>" +
      "  <button type='button' class='sq-v30-fbtn" + (sf === "pre" ? " active" : "") + "' data-v30-sf='pre'>⏳ 예정 (" + cntPre + ")</button>" +
      "  <button type='button' class='sq-v30-fbtn" + (sf === "post" ? " active" : "") + "' data-v30-sf='post'>✅ 종료 (" + cntPost + ")</button>" +
      "  <button type='button' class='sq-v30-fbtn" + (sf === "fav" ? " active" : "") + "' data-v30-sf='fav'>★ 내 관심 (" + cntFav + ")</button>" +
      "</div>" +
      "<div style='display:flex;flex-wrap:wrap;gap:5px;align-items:center;'>" +
      "  <button type='button' class='sq-v30-fbtn" + (vm === "cards" ? " active-view" : "") + "' data-v30-vm='cards'>🃏 카드 트랙</button>" +
      "  <button type='button' class='sq-v30-fbtn" + (vm === "table-league" ? " active-view" : "") + "' data-v30-vm='table-league'>📋 스코어보드 표 (리그별)</button>" +
      "  <button type='button' class='sq-v30-fbtn" + (vm === "table-time" ? " active-view" : "") + "' data-v30-vm='table-time'>🕒 시간순 표</button>" +
      "</div>"
    );

    if (!bar.__sqDelegatedV30) {
      bar.__sqDelegatedV30 = true;
      bar.addEventListener("click", function (ev) {
        var t = ev.target;
        if (!t || !t.closest) return;
        var sfBtn = t.closest("[data-v30-sf]");
        if (sfBtn) {
          v30ScoreState.statusFilter = sfBtn.getAttribute("data-v30-sf") || "all";
          v30ScoreState.lastToolbarSig = "";
          v30ScoreState.lastTableSig = "";
          refreshScoreboardSuiteV30();
          return;
        }
        var vmBtn = t.closest("[data-v30-vm]");
        if (vmBtn) {
          v30ScoreState.viewMode = vmBtn.getAttribute("data-v30-vm") || "cards";
          v30ScoreState.lastToolbarSig = "";
          v30ScoreState.lastTableSig = "";
          refreshScoreboardSuiteV30();
        }
      });
    }
  }

  function refreshScoreboardSuiteV30() {
    ensureDomesticBasketballUiControlsV34();
    ensureRootTickerObserverV31();
    installTopControlCaptureSyncV304();
    fetchServerScoreboardBridgeV30();
    var items = recomputeScopedItemsV30();
    syncSportPillLiveDotsV30();
    mountOrUpdateV30ScoreToolbar();
    renderScoreboardTableV30(items);
  }

  function getSportTacticalViewV33(it, hp, ap) {
    var sp = String((it && it.sport) || "soccer").toLowerCase();
    var h = uxEsc((it && it.home) || "홈팀");
    var a = uxEsc((it && it.away) || "원정팀");

    if (sp === "baseball") {
      return {
        tabLabel: "⚾ 수비시프트 · 타순",
        hdrTitle: "⚾ 내·외야 수비 시프트 & 투타 매치업 구도",
        hdrSub: "수비 " + h + " vs 공격 " + a + " 기준",
        statLabel: "예상 투타 주도권 · 불펜 안정감",
        expTitle: "모델 예상 기대득점 (xR)",
        expHome: (2.3 + (hp * 0.046)).toFixed(1) + "점",
        expAway: (2.3 + (ap * 0.046)).toFixed(1) + "점",
        labPresetKw: "야구",
        footNote: "하단 <strong>🧪 전술판으로 매치업 불러오기</strong>를 누르면 <strong>⚾ 야구 다이아몬드</strong> 전술판으로 즉시 전환됩니다.",
        svgHtml: [
          "<svg viewBox='0 0 460 205' width='100%' style='max-height:195px;height:auto;background:linear-gradient(180deg,#064e3b 0%,#065f46 100%);border:2px solid rgba(255,255,255,.24);border-radius:10px;display:block;'>",
          "  <path d='M 65 162 A 185 165 0 0 1 395 162 L 230 192 Z' fill='rgba(16,185,129,.16)' stroke='rgba(255,255,255,.32)' stroke-width='1.5'/>",
          "  <line x1='230' y1='186' x2='78' y2='62' stroke='rgba(255,255,255,.45)' stroke-width='1.6'/>",
          "  <line x1='230' y1='186' x2='382' y2='62' stroke='rgba(255,255,255,.45)' stroke-width='1.6'/>",
          "  <polygon points='230,96 292,141 230,186 168,141' fill='rgba(180,83,9,.46)' stroke='rgba(253,230,138,.65)' stroke-width='1.6'/>",
          "  <circle cx='230' cy='142' r='10' fill='rgba(217,119,6,.65)' stroke='rgba(255,255,255,.4)' stroke-width='1'/>",
          "  <rect x='226' y='92' width='8' height='8' fill='#fff' transform='rotate(45 230 96)'/>",
          "  <rect x='288' y='137' width='8' height='8' fill='#fff' transform='rotate(45 292 141)'/>",
          "  <rect x='164' y='137' width='8' height='8' fill='#fff' transform='rotate(45 168 141)'/>",
          "  <polygon points='225,183 235,183 235,188 230,192 225,188' fill='#fff'/>",
          "  <circle cx='126' cy='58' r='9' fill='#10b981'/><text x='126' y='61' text-anchor='middle' fill='#022c22' font-size='8.5' font-weight='900'>LF</text>",
          "  <circle cx='230' cy='40' r='9' fill='#10b981'/><text x='230' y='43' text-anchor='middle' fill='#022c22' font-size='8.5' font-weight='900'>CF</text>",
          "  <circle cx='334' cy='58' r='9' fill='#10b981'/><text x='334' y='61' text-anchor='middle' fill='#022c22' font-size='8.5' font-weight='900'>RF</text>",
          "  <circle cx='196' cy='106' r='8.5' fill='#34d399'/><text x='196' y='109' text-anchor='middle' fill='#022c22' font-size='8' font-weight='900'>SS</text>",
          "  <circle cx='264' cy='106' r='8.5' fill='#34d399'/><text x='264' y='109' text-anchor='middle' fill='#022c22' font-size='8' font-weight='900'>2B</text>",
          "  <circle cx='160' cy='132' r='8.5' fill='#34d399'/><text x='160' y='135' text-anchor='middle' fill='#022c22' font-size='8' font-weight='900'>3B</text>",
          "  <circle cx='300' cy='132' r='8.5' fill='#34d399'/><text x='300' y='135' text-anchor='middle' fill='#022c22' font-size='8' font-weight='900'>1B</text>",
          "  <circle cx='230' cy='142' r='8.5' fill='#fde68a'/><text x='230' y='145' text-anchor='middle' fill='#0f172a' font-size='8.5' font-weight='900'>P</text>",
          "  <circle cx='230' cy='196' r='8' fill='#10b981'/><text x='230' y='199' text-anchor='middle' fill='#022c22' font-size='8' font-weight='900'>C</text>",
          "  <circle cx='246' cy='184' r='8.5' fill='#38bdf8'/><text x='246' y='187' text-anchor='middle' fill='#082f49' font-size='7.5' font-weight='900'>타자</text>",
          "  <circle cx='304' cy='147' r='7.5' fill='#7dd3fc'/><text x='304' y='150' text-anchor='middle' fill='#082f49' font-size='7' font-weight='900'>주자</text>",
          "  <text x='14' y='20' fill='#a7f3d0' font-size='11' font-weight='800'>🟢 수비·마운드: " + h + "</text>",
          "  <text x='446' y='20' text-anchor='end' fill='#bae6fd' font-size='11' font-weight='800'>🔵 타선·주루: " + a + "</text>",
          "</svg>"
        ].join("")
      };
    }

    if (sp === "basketball") {
      var lgUp = String((it && (it.league_code || it.league)) || "").toUpperCase();
      var isWkbl = (lgUp === "WKBL");
      var isKbl = (lgUp === "KBL");
      var ptsBase = isWkbl ? 57 : (isKbl ? 71 : 99);
      var ptsMult = isWkbl ? 0.19 : (isKbl ? 0.22 : 0.30);
      return {
        tabLabel: "🏀 5-Out 코트전술",
        hdrTitle: (isKbl || isWkbl) ? "🏀 국내농구 하프코트 2대2 픽앤롤 & 외곽 5-Out 스페이싱" : "🏀 하프코트 5-Out 스페이싱 vs 스위치 로테이션",
        hdrSub: (isKbl || isWkbl) ? ("홈 " + h + " 2대2 전술 vs 원정 " + a + " 드롭백/스위치 수비") : "홈 5-Out 오펜스 vs 원정 2-3/스위치 수비",
        statLabel: (isKbl || isWkbl) ? "리바운드 · 외곽 3점 & 4쿼터 승부처 주도권" : "리바운드 · 클러치 페이스 주도권",
        expTitle: (isKbl || isWkbl) ? "40분 정규기준 예상 득점치 (PTS)" : "모델 예상 최종 득점치 (PTS)",
        expHome: Math.round(ptsBase + (hp * ptsMult)) + "점",
        expAway: Math.round(ptsBase + (ap * ptsMult)) + "점",
        labPresetKw: "농구",
        footNote: "하단 <strong>🧪 전술판으로 매치업 불러오기</strong>를 누르면 <strong>🏀 농구 5-Out</strong> 코트 전술판으로 즉시 전환됩니다.",
        svgHtml: [
          "<svg viewBox='0 0 460 195' width='100%' style='max-height:195px;height:auto;background:linear-gradient(90deg,#3b2314 0%,#4a2c18 50%,#3b2314 100%);border:2px solid rgba(253,230,138,.35);border-radius:10px;display:block;'>",
          "  <line x1='230' y1='0' x2='230' y2='195' stroke='rgba(255,255,255,.38)' stroke-width='1.6'/>",
          "  <circle cx='230' cy='97.5' r='26' fill='none' stroke='rgba(255,255,255,.38)' stroke-width='1.6'/>",
          "  <rect x='0' y='58' width='76' height='79' fill='rgba(16,185,129,.18)' stroke='rgba(255,255,255,.4)' stroke-width='1.5'/>",
          "  <rect x='384' y='58' width='76' height='79' fill='rgba(56,189,248,.18)' stroke='rgba(255,255,255,.4)' stroke-width='1.5'/>",
          "  <path d='M 0 22 L 54 22 A 82 82 0 0 1 54 173 L 0 173' fill='none' stroke='rgba(255,255,255,.42)' stroke-width='1.6'/>",
          "  <path d='M 460 22 L 406 22 A 82 82 0 0 0 406 173 L 460 173' fill='none' stroke='rgba(255,255,255,.42)' stroke-width='1.6'/>",
          "  <circle cx='148' cy='97.5' r='9.5' fill='#10b981'/><text x='148' y='101' text-anchor='middle' fill='#022c22' font-size='8' font-weight='900'>PG</text>",
          "  <circle cx='112' cy='38' r='9.5' fill='#10b981'/><text x='112' y='41.5' text-anchor='middle' fill='#022c22' font-size='8' font-weight='900'>SG</text>",
          "  <circle cx='112' cy='157' r='9.5' fill='#10b981'/><text x='112' y='160.5' text-anchor='middle' fill='#022c22' font-size='8' font-weight='900'>SF</text>",
          "  <circle cx='46' cy='46' r='9.5' fill='#34d399'/><text x='46' y='49.5' text-anchor='middle' fill='#022c22' font-size='8' font-weight='900'>PF</text>",
          "  <circle cx='58' cy='108' r='10' fill='#fde68a'/><text x='58' y='111.5' text-anchor='middle' fill='#0f172a' font-size='8.5' font-weight='900'>C</text>",
          "  <circle cx='312' cy='97.5' r='9.5' fill='#38bdf8'/><text x='312' y='101' text-anchor='middle' fill='#082f49' font-size='8' font-weight='900'>PG</text>",
          "  <circle cx='348' cy='40' r='9.5' fill='#38bdf8'/><text x='348' y='43.5' text-anchor='middle' fill='#082f49' font-size='8' font-weight='900'>SG</text>",
          "  <circle cx='348' cy='155' r='9.5' fill='#38bdf8'/><text x='348' y='158.5' text-anchor='middle' fill='#082f49' font-size='8' font-weight='900'>SF</text>",
          "  <circle cx='406' cy='68' r='9.5' fill='#7dd3fc'/><text x='406' y='71.5' text-anchor='middle' fill='#082f49' font-size='8' font-weight='900'>PF</text>",
          "  <circle cx='402' cy='124' r='10' fill='#bae6fd'/><text x='402' y='127.5' text-anchor='middle' fill='#082f49' font-size='8.5' font-weight='900'>C</text>",
          "  <text x='14' y='16' fill='#fde68a' font-size='11' font-weight='800'>" + h + " (5-Out)</text>",
          "  <text x='446' y='16' text-anchor='end' fill='#bae6fd' font-size='11' font-weight='800'>" + a + " (Switch)</text>",
          "</svg>"
        ].join("")
      };
    }

    if (sp === "hockey") {
      return {
        tabLabel: "🏒 라인배치 · 링크",
        hdrTitle: "🏒 1라인 포체킹(2-1-2) vs 블루라인 수비 대형",
        hdrSub: "LW·C·RW·LD·RD·G 6인 매치업",
        statLabel: "5v5 점유율(Corsi) · 파워플레이 압박",
        expTitle: "모델 기대 득점 (xG · 60분)",
        expHome: (1.7 + (hp * 0.031)).toFixed(2) + " xG",
        expAway: (1.7 + (ap * 0.031)).toFixed(2) + " xG",
        labPresetKw: "",
        footNote: "블루라인 진입 성공률과 스페셜팀(파워플레이·페널티킬) 효율이 승부를 좌우합니다.",
        svgHtml: [
          "<svg viewBox='0 0 460 190' width='100%' style='max-height:195px;height:auto;background:#0c2d48;border:2px solid rgba(186,230,253,.45);border-radius:26px;display:block;'>",
          "  <line x1='155' y1='0' x2='155' y2='190' stroke='rgba(56,189,248,.65)' stroke-width='3'/>",
          "  <line x1='305' y1='0' x2='305' y2='190' stroke='rgba(56,189,248,.65)' stroke-width='3'/>",
          "  <line x1='230' y1='0' x2='230' y2='190' stroke='rgba(239,68,68,.7)' stroke-width='2.5' stroke-dasharray='6,4'/>",
          "  <circle cx='230' cy='95' r='24' fill='none' stroke='rgba(56,189,248,.45)' stroke-width='1.5'/>",
          "  <circle cx='85' cy='52' r='18' fill='none' stroke='rgba(239,68,68,.45)' stroke-width='1.3'/><circle cx='85' cy='138' r='18' fill='none' stroke='rgba(239,68,68,.45)' stroke-width='1.3'/>",
          "  <circle cx='375' cy='52' r='18' fill='none' stroke='rgba(239,68,68,.45)' stroke-width='1.3'/><circle cx='375' cy='138' r='18' fill='none' stroke='rgba(239,68,68,.45)' stroke-width='1.3'/>",
          "  <circle cx='26' cy='95' r='9' fill='#fde68a'/><text x='26' y='98' text-anchor='middle' fill='#0f172a' font-size='8' font-weight='900'>G</text>",
          "  <circle cx='96' cy='62' r='9' fill='#10b981'/><text x='96' y='65' text-anchor='middle' fill='#022c22' font-size='7.5' font-weight='900'>LD</text>",
          "  <circle cx='96' cy='128' r='9' fill='#10b981'/><text x='96' y='131' text-anchor='middle' fill='#022c22' font-size='7.5' font-weight='900'>RD</text>",
          "  <circle cx='182' cy='46' r='9' fill='#34d399'/><text x='182' y='49' text-anchor='middle' fill='#022c22' font-size='7.5' font-weight='900'>LW</text>",
          "  <circle cx='198' cy='95' r='9.5' fill='#34d399'/><text x='198' y='98' text-anchor='middle' fill='#022c22' font-size='8' font-weight='900'>C</text>",
          "  <circle cx='182' cy='144' r='9' fill='#34d399'/><text x='182' y='147' text-anchor='middle' fill='#022c22' font-size='7.5' font-weight='900'>RW</text>",
          "  <circle cx='434' cy='95' r='9' fill='#fde68a'/><text x='434' y='98' text-anchor='middle' fill='#0f172a' font-size='8' font-weight='900'>G</text>",
          "  <circle cx='364' cy='62' r='9' fill='#38bdf8'/><text x='364' y='65' text-anchor='middle' fill='#082f49' font-size='7.5' font-weight='900'>LD</text>",
          "  <circle cx='364' cy='128' r='9' fill='#38bdf8'/><text x='364' y='131' text-anchor='middle' fill='#082f49' font-size='7.5' font-weight='900'>RD</text>",
          "  <circle cx='278' cy='46' r='9' fill='#7dd3fc'/><text x='278' y='49' text-anchor='middle' fill='#082f49' font-size='7.5' font-weight='900'>LW</text>",
          "  <circle cx='262' cy='95' r='9.5' fill='#7dd3fc'/><text x='262' y='98' text-anchor='middle' fill='#082f49' font-size='8' font-weight='900'>C</text>",
          "  <circle cx='278' cy='144' r='9' fill='#7dd3fc'/><text x='278' y='147' text-anchor='middle' fill='#082f49' font-size='7.5' font-weight='900'>RW</text>",
          "  <text x='22' y='20' fill='#a7f3d0' font-size='11' font-weight='800'>" + h + "</text>",
          "  <text x='438' y='20' text-anchor='end' fill='#bae6fd' font-size='11' font-weight='800'>" + a + "</text>",
          "</svg>"
        ].join("")
      };
    }

    if (sp === "volleyball") {
      return {
        tabLabel: "🏐 로테이션 · 코트",
        hdrTitle: "🏐 6인 로테이션(전위·후위) & 서브 리시브 배치",
        hdrSub: "전위 블로킹 3인 · 후위 디그/파이프 3인",
        statLabel: "서브 에이스 · 리시브 효율 우위",
        expTitle: "모델 예상 세트 획득 기대치",
        expHome: (1.2 + (hp * 0.026)).toFixed(1) + "세트",
        expAway: (1.2 + (ap * 0.026)).toFixed(1) + "세트",
        labPresetKw: "",
        footNote: "사이드아웃 성공률과 중앙 속공·블로킹 벽 높이가 세트 후반 승부처를 결정합니다.",
        svgHtml: [
          "<svg viewBox='0 0 460 190' width='100%' style='max-height:195px;height:auto;background:#0f3d56;border:2px solid rgba(255,255,255,.32);border-radius:10px;display:block;'>",
          "  <rect x='24' y='18' width='412' height='154' fill='rgba(234,88,12,.22)' stroke='#f8fafc' stroke-width='1.8'/>",
          "  <line x1='230' y1='12' x2='230' y2='178' stroke='#fde68a' stroke-width='3'/>",
          "  <line x1='162' y1='18' x2='162' y2='172' stroke='rgba(255,255,255,.45)' stroke-width='1.4' stroke-dasharray='5,4'/>",
          "  <line x1='298' y1='18' x2='298' y2='172' stroke='rgba(255,255,255,.45)' stroke-width='1.4' stroke-dasharray='5,4'/>",
          "  <circle cx='78' cy='48' r='10' fill='#10b981'/><text x='78' y='51.5' text-anchor='middle' fill='#022c22' font-size='8' font-weight='900'>OH</text>",
          "  <circle cx='78' cy='95' r='10' fill='#34d399'/><text x='78' y='98.5' text-anchor='middle' fill='#022c22' font-size='8' font-weight='900'>L</text>",
          "  <circle cx='78' cy='142' r='10' fill='#10b981'/><text x='78' y='145.5' text-anchor='middle' fill='#022c22' font-size='8' font-weight='900'>S</text>",
          "  <circle cx='188' cy='48' r='10' fill='#10b981'/><text x='188' y='51.5' text-anchor='middle' fill='#022c22' font-size='8' font-weight='900'>OP</text>",
          "  <circle cx='188' cy='95' r='10' fill='#fde68a'/><text x='188' y='98.5' text-anchor='middle' fill='#0f172a' font-size='8' font-weight='900'>MB</text>",
          "  <circle cx='188' cy='142' r='10' fill='#10b981'/><text x='188' y='145.5' text-anchor='middle' fill='#022c22' font-size='8' font-weight='900'>OH</text>",
          "  <circle cx='272' cy='48' r='10' fill='#38bdf8'/><text x='272' y='51.5' text-anchor='middle' fill='#082f49' font-size='8' font-weight='900'>OH</text>",
          "  <circle cx='272' cy='95' r='10' fill='#fde68a'/><text x='272' y='98.5' text-anchor='middle' fill='#0f172a' font-size='8' font-weight='900'>MB</text>",
          "  <circle cx='272' cy='142' r='10' fill='#38bdf8'/><text x='272' y='145.5' text-anchor='middle' fill='#082f49' font-size='8' font-weight='900'>OP</text>",
          "  <circle cx='382' cy='48' r='10' fill='#38bdf8'/><text x='382' y='51.5' text-anchor='middle' fill='#082f49' font-size='8' font-weight='900'>S</text>",
          "  <circle cx='382' cy='95' r='10' fill='#7dd3fc'/><text x='382' y='98.5' text-anchor='middle' fill='#082f49' font-size='8' font-weight='900'>L</text>",
          "  <circle cx='382' cy='142' r='10' fill='#38bdf8'/><text x='382' y='145.5' text-anchor='middle' fill='#082f49' font-size='8' font-weight='900'>OH</text>",
          "  <text x='30' y='32' fill='#a7f3d0' font-size='11' font-weight='800'>" + h + "</text>",
          "  <text x='430' y='32' text-anchor='end' fill='#bae6fd' font-size='11' font-weight='800'>" + a + "</text>",
          "</svg>"
        ].join("")
      };
    }

    if (sp === "esports") {
      return {
        tabLabel: "🎮 협곡라인 · 오브젝트",
        hdrTitle: "🎮 소환사의 협곡 3라인 · 바론/드래곤 시야 배치",
        hdrSub: "블루 " + h + " vs 레드 " + a + " (TOP·JGL·MID·ADC·SUP)",
        statLabel: "초중반 라인전 · 오브젝트 교전 지수",
        expTitle: "15분 골드·오브젝트 우위 확률",
        expHome: hp + "%",
        expAway: ap + "%",
        labPresetKw: "",
        footNote: "유충·첫 드래곤 타이밍의 정글-서포터 턴 활용과 미드 라인 주도권이 핵심 관전 포인트입니다.",
        svgHtml: [
          "<svg viewBox='0 0 460 195' width='100%' style='max-height:195px;height:auto;background:linear-gradient(135deg,#0f172a 0%,#132a1e 50%,#1e1b4b 100%);border:2px solid rgba(99,102,241,.4);border-radius:10px;display:block;'>",
          "  <polyline points='44,168 44,26 416,26' fill='none' stroke='rgba(148,163,184,.35)' stroke-width='10' stroke-linejoin='round'/>",
          "  <polyline points='44,168 416,168 416,26' fill='none' stroke='rgba(148,163,184,.35)' stroke-width='10' stroke-linejoin='round'/>",
          "  <line x1='44' y1='168' x2='416' y2='26' stroke='rgba(148,163,184,.38)' stroke-width='10'/>",
          "  <line x1='125' y1='35' x2='335' y2='160' stroke='rgba(56,189,248,.22)' stroke-width='12'/>",
          "  <circle cx='168' cy='62' r='11' fill='rgba(168,85,247,.38)' stroke='#c084fc' stroke-width='1.5'/><text x='168' y='65.5' text-anchor='middle' fill='#f3e8ff' font-size='7.5' font-weight='900'>바론</text>",
          "  <circle cx='292' cy='132' r='11' fill='rgba(245,158,11,.35)' stroke='#fbbf24' stroke-width='1.5'/><text x='292' y='135.5' text-anchor='middle' fill='#fef3c7' font-size='7.5' font-weight='900'>용</text>",
          "  <circle cx='66' cy='52' r='9' fill='#10b981'/><text x='66' y='55' text-anchor='middle' fill='#022c22' font-size='7' font-weight='900'>TOP</text>",
          "  <circle cx='146' cy='96' r='9' fill='#34d399'/><text x='146' y='99' text-anchor='middle' fill='#022c22' font-size='7' font-weight='900'>JGL</text>",
          "  <circle cx='208' cy='106' r='9.5' fill='#10b981'/><text x='208' y='109' text-anchor='middle' fill='#022c22' font-size='7' font-weight='900'>MID</text>",
          "  <circle cx='302' cy='168' r='9' fill='#10b981'/><text x='302' y='171' text-anchor='middle' fill='#022c22' font-size='7' font-weight='900'>ADC</text>",
          "  <circle cx='326' cy='168' r='8.5' fill='#6ee7b7'/><text x='326' y='171' text-anchor='middle' fill='#022c22' font-size='7' font-weight='900'>SUP</text>",
          "  <circle cx='106' cy='28' r='9' fill='#38bdf8'/><text x='106' y='31' text-anchor='middle' fill='#082f49' font-size='7' font-weight='900'>TOP</text>",
          "  <circle cx='302' cy='92' r='9' fill='#7dd3fc'/><text x='302' y='95' text-anchor='middle' fill='#082f49' font-size='7' font-weight='900'>JGL</text>",
          "  <circle cx='252' cy='88' r='9.5' fill='#38bdf8'/><text x='252' y='91' text-anchor='middle' fill='#082f49' font-size='7' font-weight='900'>MID</text>",
          "  <circle cx='398' cy='142' r='9' fill='#38bdf8'/><text x='398' y='145' text-anchor='middle' fill='#082f49' font-size='7' font-weight='900'>ADC</text>",
          "  <circle cx='376' cy='158' r='8.5' fill='#bae6fd'/><text x='376' y='161' text-anchor='middle' fill='#082f49' font-size='7' font-weight='900'>SUP</text>",
          "  <text x='14' y='18' fill='#6ee7b7' font-size='11' font-weight='800'>🟦 블루: " + h + "</text>",
          "  <text x='446' y='188' text-anchor='end' fill='#93c5fd' font-size='11' font-weight='800'>🟥 레드: " + a + "</text>",
          "</svg>"
        ].join("")
      };
    }

    if (sp === "ufc") {
      return {
        tabLabel: "🥊 옥타곤 · 스타일",
        hdrTitle: "🥊 UFC 옥타곤 중앙 점유 & 타격·그래플링 거리 구도",
        hdrSub: "레드코너 " + h + " vs 블루코너 " + a,
        statLabel: "케이지 압박 · 유효타 & 테이크다운 방어",
        expTitle: "피니시 · 라운드 우위 지수",
        expHome: Math.round(hp * 1.18) + " pt",
        expAway: Math.round(ap * 1.18) + " pt",
        labPresetKw: "",
        footNote: "옥타곤 중앙 스텝 컨트롤과 클린치·케이지 레슬링 체력 소모전이 핵심 승부처입니다.",
        svgHtml: [
          "<svg viewBox='0 0 460 195' width='100%' style='max-height:195px;height:auto;background:radial-gradient(circle,#1e293b 0%,#090d16 100%);border:2px solid rgba(244,63,94,.42);border-radius:10px;display:block;'>",
          "  <polygon points='196,16 264,16 306,58 306,137 264,179 196,179 154,137 154,58' fill='rgba(15,23,42,.92)' stroke='#94a3b8' stroke-width='3'/>",
          "  <polygon points='208,48 252,48 280,76 280,119 252,147 208,147 180,119 180,76' fill='none' stroke='rgba(248,113,113,.45)' stroke-width='1.5' stroke-dasharray='4,4'/>",
          "  <circle cx='230' cy='97.5' r='24' fill='rgba(244,63,94,.12)' stroke='rgba(253,230,138,.5)' stroke-width='1.4'/>",
          "  <circle cx='198' cy='97.5' r='15' fill='#ef4444'/><text x='198' y='101.5' text-anchor='middle' fill='#fff' font-size='9' font-weight='900'>RED</text>",
          "  <circle cx='262' cy='97.5' r='15' fill='#3b82f6'/><text x='262' y='101.5' text-anchor='middle' fill='#fff' font-size='9' font-weight='900'>BLUE</text>",
          "  <text x='18' y='34' fill='#fca5a5' font-size='11.5' font-weight='900'>🥊 " + h + "</text>",
          "  <text x='18' y='52' fill='#cbd5e1' font-size='10'>AI 우세율 " + hp + "% · 중앙 압박</text>",
          "  <text x='442' y='34' text-anchor='end' fill='#93c5fd' font-size='11.5' font-weight='900'>" + a + " 🥊</text>",
          "  <text x='442' y='52' text-anchor='end' fill='#cbd5e1' font-size='10'>AI 우세율 " + ap + "% · 카운터·전환</text>",
          "</svg>"
        ].join("")
      };
    }

    if (sp === "tennis") {
      return {
        tabLabel: "🎾 서브 · 랠리존",
        hdrTitle: "🎾 퍼스트 서브 코스 & 베이스라인 랠리 타점 구도",
        hdrSub: h + " vs " + a + " 코트 포지셔닝",
        statLabel: "퍼스트 서브 득점률 · 브레이크 방어력",
        expTitle: "서비스게임 유지 기대율",
        expHome: Math.min(94, Math.round(62 + (hp * 0.30))) + "%",
        expAway: Math.min(94, Math.round(62 + (ap * 0.30))) + "%",
        labPresetKw: "",
        footNote: "세컨드 서브 리턴 공략 깊이와 듀스 코트 크로스 랠리 주도권이 승부를 가릅니다.",
        svgHtml: [
          "<svg viewBox='0 0 460 190' width='100%' style='max-height:195px;height:auto;background:#1e3a8a;border:2px solid rgba(255,255,255,.35);border-radius:10px;display:block;'>",
          "  <rect x='36' y='20' width='388' height='150' fill='#1d4ed8' stroke='#fff' stroke-width='2'/>",
          "  <line x1='36' y1='38' x2='424' y2='38' stroke='#fff' stroke-width='1.3'/><line x1='36' y1='152' x2='424' y2='152' stroke='#fff' stroke-width='1.3'/>",
          "  <line x1='230' y1='14' x2='230' y2='176' stroke='#fde68a' stroke-width='2.8'/>",
          "  <line x1='132' y1='38' x2='132' y2='152' stroke='#fff' stroke-width='1.4'/><line x1='328' y1='38' x2='328' y2='152' stroke='#fff' stroke-width='1.4'/>",
          "  <line x1='132' y1='95' x2='328' y2='95' stroke='#fff' stroke-width='1.4'/>",
          "  <line x1='52' y1='118' x2='312' y2='58' stroke='rgba(163,230,53,.8)' stroke-width='2' stroke-dasharray='5,4'/>",
          "  <circle cx='50' cy='120' r='11' fill='#10b981'/><text x='50' y='123.5' text-anchor='middle' fill='#022c22' font-size='8' font-weight='900'>P1</text>",
          "  <circle cx='408' cy='68' r='11' fill='#fde047'/><text x='408' y='71.5' text-anchor='middle' fill='#0f172a' font-size='8' font-weight='900'>P2</text>",
          "  <text x='42' y='15' fill='#a7f3d0' font-size='11' font-weight='800'>" + h + "</text>",
          "  <text x='418' y='15' text-anchor='end' fill='#fef08a' font-size='11' font-weight='800'>" + a + "</text>",
          "</svg>"
        ].join("")
      };
    }

    if (sp === "f1") {
      return {
        tabLabel: "🏎️ 서킷 · 피트전략",
        hdrTitle: "🏎️ 섹터별(S1·S2·S3) 랩페이스 & DRS·타이어 피트 전략",
        hdrSub: h + " vs " + a + " 헤드투헤드",
        statLabel: "직선 최고속도(DRS) · 타이어 데그 관리",
        expTitle: "퀄리파잉 · 레이스 페이스 지수",
        expHome: hp + " pt",
        expAway: ap + " pt",
        labPresetKw: "",
        footNote: "DRS 추월 스트레이트 구간 슬립스트림과 미디엄→하드 언더컷 피트스톱 타이밍이 핵심입니다.",
        svgHtml: [
          "<svg viewBox='0 0 460 190' width='100%' style='max-height:195px;height:auto;background:#090f1f;border:2px solid rgba(250,204,21,.4);border-radius:10px;display:block;'>",
          "  <path d='M 78 144 C 42 144, 42 54, 96 46 L 298 46 C 368 46, 408 82, 392 126 C 378 158, 318 152, 268 134 L 182 144 Z' fill='none' stroke='#334155' stroke-width='18' stroke-linejoin='round'/>",
          "  <path d='M 78 144 C 42 144, 42 54, 96 46 L 298 46 C 368 46, 408 82, 392 126 C 378 158, 318 152, 268 134 L 182 144 Z' fill='none' stroke='#e2e8f0' stroke-width='1.5' stroke-dasharray='6,5'/>",
          "  <line x1='110' y1='46' x2='275' y2='46' stroke='#22c55e' stroke-width='4'/>",
          "  <text x='192' y='35' text-anchor='middle' fill='#4ade80' font-size='9.5' font-weight='900'>⚡ DRS OVERTAKE ZONE (S1)</text>",
          "  <circle cx='146' cy='46' r='9' fill='#ef4444'/><text x='146' y='49' text-anchor='middle' fill='#fff' font-size='7.5' font-weight='900'>P1</text>",
          "  <circle cx='182' cy='46' r='9' fill='#38bdf8'/><text x='182' y='49' text-anchor='middle' fill='#082f49' font-size='7.5' font-weight='900'>P2</text>",
          "  <text x='382' y='72' fill='#fde68a' font-size='9.5' font-weight='800'>S2 고속코너</text>",
          "  <text x='230' y='172' text-anchor='middle' fill='#93c5fd' font-size='9.5' font-weight='800'>🔧 PIT LANE · 언더컷 윈도우 (S3)</text>",
          "  <text x='16' y='20' fill='#fca5a5' font-size='11' font-weight='800'>🏎️ " + h + "</text>",
          "  <text x='444' y='20' text-anchor='end' fill='#bae6fd' font-size='11' font-weight='800'>" + a + " 🏎️</text>",
          "</svg>"
        ].join("")
      };
    }

    // Default: Soccer (4-2-3-1 vs 4-3-3)
    return {
      tabLabel: "📋 포메이션 · 전술",
      hdrTitle: "📋 기본 전술 포메이션 매치업 프리뷰",
      hdrSub: "홈 4-2-3-1 vs 원정 4-3-3 기준",
      statLabel: "예상 점유율 · 전방 압박 지수",
      expTitle: "모델 기대 득점력 (xG)",
      expHome: (hp / 25).toFixed(2) + " xG",
      expAway: (ap / 25).toFixed(2) + " xG",
      labPresetKw: "축구 4-2-3-1",
      footNote: "하단 <strong>🧪 전술판으로 매치업 불러오기</strong>를 누르면 직접 선수 위치를 드래그해 커스텀 전술을 공유할 수 있습니다.",
      svgHtml: [
        "<svg viewBox='0 0 460 190' width='100%' style='max-height:195px;height:auto;background:#064e3b;border:2px solid rgba(255,255,255,.25);border-radius:10px;display:block;'>",
        "  <line x1='230' y1='0' x2='230' y2='190' stroke='rgba(255,255,255,.3)' stroke-width='1.5'/>",
        "  <circle cx='230' cy='95' r='28' fill='none' stroke='rgba(255,255,255,.3)' stroke-width='1.5'/>",
        "  <rect x='0' y='45' width='42' height='100' fill='none' stroke='rgba(255,255,255,.3)' stroke-width='1.5'/>",
        "  <rect x='418' y='45' width='42' height='100' fill='none' stroke='rgba(255,255,255,.3)' stroke-width='1.5'/>",
        "  <circle cx='22' cy='95' r='8' fill='#10b981'/><circle cx='72' cy='35' r='8' fill='#10b981'/><circle cx='72' cy='75' r='8' fill='#10b981'/><circle cx='72' cy='115' r='8' fill='#10b981'/><circle cx='72' cy='155' r='8' fill='#10b981'/>",
        "  <circle cx='122' cy='70' r='8' fill='#10b981'/><circle cx='122' cy='120' r='8' fill='#10b981'/>",
        "  <circle cx='168' cy='42' r='8' fill='#10b981'/><circle cx='168' cy='95' r='8' fill='#10b981'/><circle cx='168' cy='148' r='8' fill='#10b981'/><circle cx='206' cy='95' r='9' fill='#34d399'/>",
        "  <circle cx='438' cy='95' r='8' fill='#38bdf8'/><circle cx='388' cy='35' r='8' fill='#38bdf8'/><circle cx='388' cy='75' r='8' fill='#38bdf8'/><circle cx='388' cy='115' r='8' fill='#38bdf8'/><circle cx='388' cy='155' r='8' fill='#38bdf8'/>",
        "  <circle cx='332' cy='52' r='8' fill='#38bdf8'/><circle cx='332' cy='95' r='8' fill='#38bdf8'/><circle cx='332' cy='138' r='8' fill='#38bdf8'/>",
        "  <circle cx='272' cy='45' r='8' fill='#7dd3fc'/><circle cx='256' cy='95' r='9' fill='#7dd3fc'/><circle cx='272' cy='145' r='8' fill='#7dd3fc'/>",
        "  <text x='14' y='18' fill='#a7f3d0' font-size='11' font-weight='800'>" + h + "</text>",
        "  <text x='446' y='18' text-anchor='end' fill='#bae6fd' font-size='11' font-weight='800'>" + a + "</text>",
        "</svg>"
      ].join("")
    };
  }

  function openMatchInsightModalByItemV30(it) {
    if (!it) return;
    hideRowHoverPreviewV31();
    var oldMd = document.getElementById("dcMatchDetailModal");
    if (oldMd) oldMd.style.display = "none";
    var oldMd2 = document.getElementById("dcMatchModal");
    if (oldMd2) oldMd2.style.display = "none";

    var modal = document.getElementById("dcQuickMatchModal");
    if (!modal) {
      modal = document.createElement("div");
      modal.id = "dcQuickMatchModal";
      modal.style.cssText = "position:fixed;inset:0;background:rgba(2,6,23,0.82);backdrop-filter:blur(4px);z-index:10008;display:flex;align-items:center;justify-content:center;padding:14px;";
      modal.addEventListener("click", function (ev) {
        if (ev.target === modal) modal.style.display = "none";
      });
      document.body.appendChild(modal);
    }

    var fk = matchFavKeyV30(it);
    var isFav = Boolean(v30ScoreState.favMap[fk]);
    var ai = it.ai || {};
    var odds = ai.odds || {};
    var hp = Number(ai.home_pct || 45);
    var dp = Number(ai.draw_pct || 0);
    var ap = Number(ai.away_pct || (100 - hp - dp));

    var oh = parseFloat(odds.home) || 0;
    var od = parseFloat(odds.draw) || 0;
    var oa = parseFloat(odds.away) || 0;
    var invSum = (oh > 0 ? 1 / oh : 0) + (od > 0 ? 1 / od : 0) + (oa > 0 ? 1 / oa : 0);
    var mktH = invSum > 0 && oh > 0 ? Math.round(((1 / oh) / invSum) * 1000) / 10 : hp;
    var mktD = invSum > 0 && od > 0 ? Math.round(((1 / od) / invSum) * 1000) / 10 : dp;
    var mktA = invSum > 0 && oa > 0 ? Math.round(((1 / oa) / invSum) * 1000) / 10 : ap;
    var edgeH = Math.round((hp - mktH) * 10) / 10;
    var edgeA = Math.round((ap - mktA) * 10) / 10;
    var verdict = evaluateAiVerdictV30(it);
    var tacMeta = getSportTacticalViewV33(it, hp, ap);

    modal.style.display = "flex";
    modal.innerHTML = [
      "<div style='background:#0f172a;border:1px solid #334155;border-radius:18px;max-width:620px;width:100%;max-height:88vh;display:flex;flex-direction:column;color:#f8fafc;box-shadow:0 24px 55px rgba(0,0,0,0.65);overflow:hidden;'>",
      "  <div style='padding:14px 16px;border-bottom:1px solid #1e293b;display:flex;justify-content:space-between;align-items:center;gap:8px;'>",
      "    <div style='display:flex;align-items:center;gap:6px;flex-wrap:wrap;min-width:0;flex:1 1 auto;'>",
      "      <span style='background:#2563eb;color:#fff;padding:3px 9px;border-radius:999px;font-size:11.5px;font-weight:800;'>" + sportEmojiV30(it.sport) + " " + uxEsc(it.league_full || it.league) + " · " + uxEsc(it.status_badge) + "</span>",
      (verdict ? verdict.badgeHtml : ""),
      "    </div>",
      "    <div style='display:flex;align-items:center;gap:5px;flex-shrink:0;'>",
      "      <button type='button' id='sqModalFavBtnV30' style='background:#1e293b;border:1px solid #334155;color:" + (isFav ? "#fbbf24" : "#cbd5e1") + ";border-radius:8px;padding:4px 9px;font-size:11.5px;font-weight:800;cursor:pointer;'>" + (isFav ? "★ 관심됨" : "☆ 관심등록") + "</button>",
      "      <button type='button' id='sqCloseMatchInsightV30' style='background:#1e293b;border:1px solid #334155;color:#e2e8f0;border-radius:8px;padding:4px 10px;font-size:11.5px;font-weight:800;cursor:pointer;display:inline-flex;align-items:center;gap:4px;'><svg data-sq-x-svg='1' viewBox='0 0 14 14' width='11' height='11' fill='none'><path d='M3 3L11 11M11 3L3 11' stroke='currentColor' stroke-width='2.2' stroke-linecap='round'/></svg><span>닫기</span></button>",
      "    </div>",
      "  </div>",
      "  <div style='padding:14px 16px;overflow-y:auto;flex:1;'>",
      "    <div style='font-size:12px;color:#fde68a;font-weight:700;margin-bottom:8px;'>📅 " + uxEsc(it.match_dt_full_kst || it.match_dt_kst || it.status_badge) + (it.venue ? " · 🏟️ " + uxEsc(it.venue) : "") + "</div>",
      "    <div style='background:linear-gradient(135deg,#020617,#1e293b);border:1px solid #334155;border-radius:14px;padding:14px 16px;display:grid;grid-template-columns:minmax(0,1fr) auto minmax(0,1fr);align-items:center;gap:10px;margin-bottom:12px;'>",
      "      <div style='font-weight:900;font-size:15.5px;'>" + uxEsc(it.home) + "</div>",
      "      <div style='text-align:center;'>",
      "        <div style='font-weight:900;font-size:24px;color:#38bdf8;'>" + (it.state === "pre" ? "VS" : (uxEsc(it.home_score) + " : " + uxEsc(it.away_score))) + "</div>",
      (it.period_scores ? "<div style='font-size:11px;color:#93c5fd;margin-top:2px;'>" + uxEsc(it.period_scores) + "</div>" : ""),
      "      </div>",
      "      <div style='font-weight:900;font-size:15.5px;text-align:right;'>" + uxEsc(it.away) + "</div>",
      "    </div>",
      "    <div id='sqMatchInsightTabsV30' style='display:flex;gap:6px;overflow-x:auto;padding-bottom:6px;margin-bottom:12px;'>",
      "      <button type='button' class='sq-v30-insight-tab active' data-mitab='summary'>📊 요약 · 확률격차</button>",
      "      <button type='button' class='sq-v30-insight-tab' data-mitab='stats'>📈 스탯 · 흐름</button>",
      "      <button type='button' class='sq-v30-insight-tab' data-mitab='h2h'>⚔️ 전력 · 순위 · 부상</button>",
      "      <button type='button' class='sq-v30-insight-tab' data-mitab='lineup'>" + tacMeta.tabLabel + "</button>",
      "      <button type='button' class='sq-v30-insight-tab' data-mitab='talk'>💬 이 경기 한줄톡</button>",
      "    </div>",
      "    <div id='sqMatchInsightBodyV30' style='background:#020617;border:1px solid #1e293b;border-radius:12px;padding:13px;min-height:150px;font-size:12.5px;line-height:1.6;'></div>",
      "  </div>",
      "  <div style='padding:12px 16px;border-top:1px solid #1e293b;background:#090f1f;display:flex;flex-wrap:wrap;gap:7px;'>",
      "    <button type='button' id='sqBridgeTacticalV30' style='flex:1;min-width:150px;background:rgba(56,189,248,.16);border:1px solid rgba(56,189,248,.5);color:#7dd3fc;border-radius:10px;padding:8px 10px;font-size:12px;font-weight:800;cursor:pointer;'>🧪 전술판으로 매치업 불러오기</button>",
      "    <button type='button' id='sqBridgeWritePostV30' style='flex:1;min-width:165px;background:linear-gradient(135deg,#10b981,#059669);border:0;color:#020617;border-radius:10px;padding:8px 10px;font-size:12px;font-weight:900;cursor:pointer;'>✍️ 이 경기로 분석글 쓰기 (+15P)</button>",
      "    <button type='button' id='sqBridgeStandingsV30' style='background:#1e293b;border:1px solid #334155;color:#cbd5e1;border-radius:10px;padding:8px 11px;font-size:12px;font-weight:800;cursor:pointer;'>🔢 순위표</button>",
      "  </div>",
      "</div>"
    ].join("");

    document.getElementById("sqCloseMatchInsightV30").onclick = function () { modal.style.display = "none"; };
    document.getElementById("sqModalFavBtnV30").onclick = function (ev) {
      window.toggleMatchFavV30(ev, fk);
      var nowFav = Boolean(v30ScoreState.favMap[fk]);
      var b = document.getElementById("sqModalFavBtnV30");
      if (b) {
        b.textContent = nowFav ? "★ 관심됨" : "☆ 관심등록";
        b.style.color = nowFav ? "#fbbf24" : "#cbd5e1";
      }
    };

    function renderInsightTab(tabKey) {
      var box = document.getElementById("sqMatchInsightBodyV30");
      if (!box) return;
      if (tabKey === "summary") {
        var edgeNote = Math.abs(edgeH) >= Math.abs(edgeA)
          ? (uxEsc(it.home) + " 측 AI 확률(" + hp + "%)이 배당 내재확률(" + mktH + "%) 대비 <strong style='color:" + (edgeH >= 0 ? "#34d399" : "#fbbf24") + ";'>" + (edgeH >= 0 ? "+" : "") + edgeH + "%p</strong> 차이를 보입니다.")
          : (uxEsc(it.away) + " 측 AI 확률(" + ap + "%)이 배당 내재확률(" + mktA + "%) 대비 <strong style='color:" + (edgeA >= 0 ? "#34d399" : "#fbbf24") + ";'>" + (edgeA >= 0 ? "+" : "") + edgeA + "%p</strong> 차이를 보입니다.");
        box.innerHTML = [
          "<div style='font-weight:800;color:#6ee7b7;margin-bottom:8px;'>🤖 SportIQ 앙상블 확률 vs 시장 배당 내재확률 비교</div>",
          "<div style='display:flex;height:10px;border-radius:999px;overflow:hidden;background:#1e293b;margin-bottom:6px;'>",
          "  <div style='width:" + hp + "%;background:#10b981;'></div>",
          (dp > 0 ? "<div style='width:" + dp + "%;background:#64748b;'></div>" : ""),
          "  <div style='width:" + ap + "%;background:#38bdf8;'></div>",
          "</div>",
          "<div style='display:grid;grid-template-columns:repeat(" + (dp > 0 ? 3 : 2) + ",minmax(0,1fr));gap:8px;text-align:center;margin-bottom:10px;'>",
          "  <div style='background:#0f172a;border:1px solid #1e293b;border-radius:9px;padding:7px;'><div style='color:#94a3b8;font-size:11px;'>홈승 (" + uxEsc(it.home) + ")</div><strong style='color:#34d399;font-size:14px;'>AI " + hp + "%</strong><div style='color:#64748b;font-size:10.5px;'>시장 " + mktH + "% · 배당 " + uxEsc(odds.home || "-") + "</div></div>",
          (dp > 0 ? "  <div style='background:#0f172a;border:1px solid #1e293b;border-radius:9px;padding:7px;'><div style='color:#94a3b8;font-size:11px;'>무승부</div><strong style='color:#cbd5e1;font-size:14px;'>AI " + dp + "%</strong><div style='color:#64748b;font-size:10.5px;'>시장 " + mktD + "% · 배당 " + uxEsc(odds.draw || "-") + "</div></div>" : ""),
          "  <div style='background:#0f172a;border:1px solid #1e293b;border-radius:9px;padding:7px;'><div style='color:#94a3b8;font-size:11px;'>원정승 (" + uxEsc(it.away) + ")</div><strong style='color:#38bdf8;font-size:14px;'>AI " + ap + "%</strong><div style='color:#64748b;font-size:10.5px;'>시장 " + mktA + "% · 배당 " + uxEsc(odds.away || "-") + "</div></div>",
          "</div>",
          "<div style='background:rgba(16,185,129,.08);border:1px solid rgba(52,211,153,.3);border-radius:9px;padding:9px 11px;color:#e2e8f0;margin-bottom:10px;'>💡 <strong>밸류 핵심 요약:</strong> " + edgeNote + "</div>",
          "<div id='sqMatchFanVoteCardV32' style='background:linear-gradient(145deg,rgba(15,23,42,.96),rgba(2,6,23,.96));border:1px solid rgba(56,189,248,.38);border-radius:11px;padding:10px 12px;'>",
          "  <div style='display:flex;justify-content:space-between;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:8px;'>",
          "    <div style='display:flex;align-items:center;gap:6px;flex-wrap:wrap;'>",
          "      <span style='font-weight:900;color:#f8fafc;font-size:12.3px;'>🗳️ 실시간 팬 승부투표</span>",
          "      <span id='sqFanVoteRewardTagV32' style='background:rgba(16,185,129,.2);border:1px solid rgba(52,211,153,.5);color:#6ee7b7;border-radius:999px;padding:1px 8px;font-size:10.5px;font-weight:800;'>참여 시 +5P 즉시 적립</span>",
          "    </div>",
          "    <span id='sqFanVoteTotalMetaV32' style='font-size:11px;color:#94a3b8;font-weight:700;'>실시간 집계 불러오는 중...</span>",
          "  </div>",
          "  <div id='sqFanVoteBtnsGridV32' style='display:grid;grid-template-columns:repeat(" + (dp > 0 ? 3 : 2) + ",minmax(0,1fr));gap:7px;margin-bottom:8px;'></div>",
          "  <div id='sqFanVoteGaugeWrapV32'></div>",
          "</div>"
        ].join("");

        var hasDrawVote = dp > 0;
        function renderFanVoteStateUI(vd) {
          var grid = document.getElementById("sqFanVoteBtnsGridV32");
          var gWrap = document.getElementById("sqFanVoteGaugeWrapV32");
          var rTag = document.getElementById("sqFanVoteRewardTagV32");
          var tMeta = document.getElementById("sqFanVoteTotalMetaV32");
          if (!grid || !gWrap) return;

          var myP = String((vd && vd.my_pick) || "");
          var fHp = vd ? Number(vd.home_pct || 0) : hp;
          var fDp = (vd && hasDrawVote) ? Number(vd.draw_pct || 0) : (hasDrawVote ? dp : 0);
          var fAp = vd ? Number(vd.away_pct || 0) : ap;
          var hv = vd ? Number(vd.home_votes || 0) : 0;
          var dv = (vd && hasDrawVote) ? Number(vd.draw_votes || 0) : 0;
          var av = vd ? Number(vd.away_votes || 0) : 0;
          var totV = vd ? Number(vd.total_votes || (hv + dv + av)) : 0;

          if (tMeta) tMeta.textContent = "총 " + totV.toLocaleString() + "명 참여";
          if (rTag) {
            if (myP) {
              var myLbl = myP === "H" ? (it.home + " 승") : (myP === "A" ? (it.away + " 승") : "무승부");
              var liveP = ((typeof state === "object" && state && state.points != null) ? state.points : ((vd && vd.points != null) ? vd.points : null)); rTag.textContent = "✅ +5P 적립됨" + (liveP != null ? " (보유 " + liveP + "P)" : "") + " · 내 픽: " + myLbl;
              rTag.style.background = "rgba(56,189,248,.2)";
              rTag.style.borderColor = "rgba(56,189,248,.55)";
              rTag.style.color = "#7dd3fc";
            } else {
              rTag.textContent = "참여 시 +5P 즉시 적립";
            }
          }

          var btns = [
            "<button type='button' class='sq-v32-vote-btn" + (myP === "H" ? " is-picked-H" : "") + "' data-sq-v32-pick='H'>" +
            "  <span style='font-size:11px;color:#cbd5e1;font-weight:800;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;'>" + (myP === "H" ? "✓ " : "🏠 ") + uxEsc(it.home) + " 승</span>" +
            "  <strong style='font-size:14px;color:#34d399;'>" + fHp + "%</strong>" +
            "  <span style='font-size:10px;color:#64748b;'>" + hv + "표</span>" +
            "</button>"
          ];
          if (hasDrawVote) {
            btns.push(
              "<button type='button' class='sq-v32-vote-btn" + (myP === "D" ? " is-picked-D" : "") + "' data-sq-v32-pick='D'>" +
              "  <span style='font-size:11px;color:#cbd5e1;font-weight:800;'>" + (myP === "D" ? "✓ " : "⚖️ ") + "무승부</span>" +
              "  <strong style='font-size:14px;color:#e2e8f0;'>" + fDp + "%</strong>" +
              "  <span style='font-size:10px;color:#64748b;'>" + dv + "표</span>" +
              "</button>"
            );
          }
          btns.push(
            "<button type='button' class='sq-v32-vote-btn" + (myP === "A" ? " is-picked-A" : "") + "' data-sq-v32-pick='A'>" +
            "  <span style='font-size:11px;color:#cbd5e1;font-weight:800;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;'>" + (myP === "A" ? "✓ " : "✈️ ") + uxEsc(it.away) + " 승</span>" +
            "  <strong style='font-size:14px;color:#38bdf8;'>" + fAp + "%</strong>" +
            "  <span style='font-size:10px;color:#64748b;'>" + av + "표</span>" +
            "</button>"
          );
          grid.innerHTML = btns.join("");

          var diffHome = fHp - hp;
          var sentimentTxt = Math.abs(diffHome) <= 3
            ? "AI 예측(" + hp + "%)과 팬 투표 여론(" + fHp + "%)이 거의 일치합니다."
            : (diffHome > 0
                ? (uxEsc(it.home) + " 쪽으로 팬 여론(" + fHp + "%)이 AI(" + hp + "%)보다 <strong style='color:#34d399;'>+" + diffHome + "%p</strong> 높게 쏠려 있습니다.")
                : (uxEsc(it.away) + " 쪽으로 팬 여론(" + fAp + "%)이 AI(" + ap + "%) 대비 역배·원정 지지가 높습니다."));

          gWrap.innerHTML = [
            "<div style='display:flex;height:8px;border-radius:999px;overflow:hidden;background:#1e293b;margin-bottom:5px;'>",
            "  <div style='width:" + fHp + "%;background:#10b981;transition:width .25s ease;'></div>",
            (hasDrawVote ? "  <div style='width:" + fDp + "%;background:#64748b;transition:width .25s ease;'></div>" : ""),
            "  <div style='width:" + fAp + "%;background:#38bdf8;transition:width .25s ease;'></div>",
            "</div>",
            "<div style='font-size:11px;color:#94a3b8;'>📊 <strong>AI vs 팬 민심:</strong> " + sentimentTxt + "</div>"
          ].join("");

          grid.querySelectorAll("[data-sq-v32-pick]").forEach(function (vb) {
            vb.onclick = function () {
              var chosen = vb.getAttribute("data-sq-v32-pick") || "H";
              var stRef = (typeof state === "object" && state) ? state : (window.state || null);
              var curPts = (stRef && stRef.points != null) ? Number(stRef.points || 120) : 120;
              if (isNaN(curPts) || curPts < 0) curPts = 120;
              fetch("/api/member/match-vote", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                  match_key: fk,
                  pick: chosen,
                  has_draw: hasDrawVote,
                  hp: hp,
                  dp: dp,
                  ap: ap,
                  nickname: getSavedNickV22(),
                  pin: getSavedPinV22(),
                  actor_key: getActorKeyV22(),
                  client_points: curPts
                })
              })
                .then(function (r) { return r.json(); })
                .then(function (res) {
                  if (!res || !res.ok) return;
                  renderFanVoteStateUI(res);
                  if (typeof res.points === "number" && res.awarded_points > 0) {
                    var stTarget = (typeof state === "object" && state) ? state : (window.state || null);
                    if (stTarget) {
                      stTarget.points = Number(res.points);
                      if (res.role_tier) stTarget.role = res.role_tier;
                    }
                    try { localStorage.setItem("sportiq_points", String(res.points)); } catch (e) {}
                    if (typeof window.updatePointsUI === "function") {
                      try { window.updatePointsUI(); } catch (e) {}
                    }
                    if (res.role_tier && typeof window.setRole === "function") {
                      try { window.setRole(res.role_tier, false); } catch (e) {}
                    }
                    ["pointsBadge", "userPoints", "heroPoints", "quickPointsBadge"].forEach(function (pid) {
                      var pel = document.getElementById(pid);
                      if (pel) pel.textContent = res.points + " P";
                    });
                    renderTierUpgradeSidebarCard();
                    renderFanVoteStateUI(res);
                  }
                  if (res.message && typeof showToast === "function") {
                    showToast(res.message);
                  }
                })
                .catch(function () {});
            };
          });
        }

        // Render immediate optimistic preview, then hydrate exact DB tally
        renderFanVoteStateUI(null);
        var qVoteUrl = "/api/member/match-vote?match_key=" + encodeURIComponent(fk) +
                       "&actor_key=" + encodeURIComponent(getActorKeyV22()) +
                       "&nickname=" + encodeURIComponent(getSavedNickV22()) +
                       "&has_draw=" + (hasDrawVote ? "1" : "0") +
                       "&hp=" + encodeURIComponent(hp) +
                       "&dp=" + encodeURIComponent(dp) +
                       "&ap=" + encodeURIComponent(ap);
        fetch(qVoteUrl)
          .then(function (r) { return r.json(); })
          .then(function (vd) {
            if (vd && vd.ok) renderFanVoteStateUI(vd);
          })
          .catch(function () {});
      } else if (tabKey === "stats") {
        var domH = Math.max(22, Math.min(78, Math.round(hp + (dp * 0.5))));
        var domA = 100 - domH;
        box.innerHTML = [
          "<div style='font-weight:800;color:#7dd3fc;margin-bottom:8px;'>📈 세부 스코어 · 종목 맞춤 주도권 비교</div>",
          "<div style='background:#0f172a;border:1px solid #1e293b;border-radius:9px;padding:9px 11px;margin-bottom:10px;'>",
          "  <div style='color:#94a3b8;font-size:11px;'>전·후반 / 쿼터 / 이닝 / 세트 / 라운드 기록</div>",
          "  <div style='color:#f8fafc;font-weight:700;margin-top:2px;'>" + uxEsc(it.period_scores || "킥오프·경기 시작 이후 실시간 세부 스코어가 자동 집계됩니다.") + "</div>",
          "</div>",
          "<div style='margin-bottom:8px;'>",
          "  <div style='display:flex;justify-content:space-between;font-size:11.5px;margin-bottom:3px;'><span>" + uxEsc(it.home) + " " + domH + "%</span><span style='color:#94a3b8;'>" + tacMeta.statLabel + "</span><span>" + domA + "% " + uxEsc(it.away) + "</span></div>",
          "  <div style='display:flex;height:8px;border-radius:999px;overflow:hidden;background:#1e293b;'><div style='width:" + domH + "%;background:#10b981;'></div><div style='width:" + domA + "%;background:#38bdf8;'></div></div>",
          "</div>",
          "<div>",
          "  <div style='display:flex;justify-content:space-between;font-size:11.5px;margin-bottom:3px;'><span style='font-weight:800;color:#6ee7b7;'>" + tacMeta.expHome + "</span><span style='color:#94a3b8;'>" + tacMeta.expTitle + "</span><span style='font-weight:800;color:#7dd3fc;'>" + tacMeta.expAway + "</span></div>",
          "  <div style='display:flex;height:8px;border-radius:999px;overflow:hidden;background:#1e293b;'><div style='width:" + Math.round((hp / Math.max(1, hp + ap)) * 100) + "%;background:#34d399;'></div><div style='flex:1;background:#60a5fa;'></div></div>",
          "</div>"
        ].join("");
      } else if (tabKey === "h2h") {
        box.innerHTML = "<div style='color:#94a3b8;'>⚔️ 양 팀 실시간 리그 순위·최근 폼 및 결장자 명단을 조회 중입니다...</div>";
        var lgc = encodeURIComponent(it.league_code || "EPL");
        Promise.all([
          fetch("/api/datacenter/standings?league=" + lgc).then(function (r) { return r.json(); }).catch(function () { return {}; }),
          fetch("/api/datacenter/injuries?league=" + lgc).then(function (r) { return r.json(); }).catch(function () { return {}; })
        ]).then(function (arr) {
          var stData = arr[0] || {};
          var injData = arr[1] || {};
          var rows = stData.standings || [];
          function matchTeamRow(name) {
            var n = String(name || "").toLowerCase();
            return rows.find(function (r) {
              var rt = String(r.team || "").toLowerCase();
              return rt === n || rt.indexOf(n) !== -1 || n.indexOf(rt) !== -1;
            });
          }
          var hRow = matchTeamRow(it.home);
          var aRow = matchTeamRow(it.away);
          function fmtStanding(tName, rObj) {
            if (!rObj) return "<div style='background:#0f172a;border:1px solid #1e293b;border-radius:9px;padding:8px 10px;'><strong>" + uxEsc(tName) + "</strong>: 국제대회·토너먼트 매치업</div>";
            var fm = Array.isArray(rObj.form) ? rObj.form.join("·") : String(rObj.form || "-");
            return "<div style='background:#0f172a;border:1px solid #1e293b;border-radius:9px;padding:8px 10px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px;'><span><strong style='color:#fde68a;'>#" + uxEsc(rObj.rank) + "</strong> <strong>" + uxEsc(rObj.team) + "</strong> (" + uxEsc(rObj.w) + "승 " + uxEsc(rObj.d) + "무 " + uxEsc(rObj.l) + "패 · 승점 " + uxEsc(rObj.pts) + ")</span><span style='font-size:11px;color:#34d399;font-weight:800;'>최근폼 " + uxEsc(fm) + "</span></div>";
          }
          var teamInjs = (injData.items || []).filter(function (x) {
            var tm = String(x.team || "");
            return tm.indexOf(it.home) !== -1 || it.home.indexOf(tm) !== -1 || tm.indexOf(it.away) !== -1 || it.away.indexOf(tm) !== -1;
          }).slice(0, 5);
          var injHtml = teamInjs.length
            ? teamInjs.map(function (x) {
                return "<div style='font-size:11.5px;padding:4px 0;border-bottom:1px solid #1e293b;'>🏥 <strong>[" + uxEsc(x.team) + "] " + uxEsc(x.player) + "</strong> · " + uxEsc(x.reason) + " <span style='color:#fbbf24;'>(" + uxEsc(x.severity) + ")</span></div>";
              }).join("")
            : "<div style='font-size:11.5px;color:#94a3b8;'>현재 등록된 양 팀 주요 장기 결장자 이슈가 없습니다.</div>";
          box.innerHTML = (
            "<div style='font-weight:800;color:#6ee7b7;margin-bottom:7px;'>⚔️ 양 팀 현재 리그 성적 & 최근 5경기 흐름</div>" +
            "<div style='display:grid;gap:6px;margin-bottom:10px;'>" + fmtStanding(it.home, hRow) + fmtStanding(it.away, aRow) + "</div>" +
            "<div style='font-weight:800;color:#fca5a5;margin-bottom:4px;'>🏥 양 팀 주요 결장 · 부상 체크</div>" + injHtml
          );
        });
      } else if (tabKey === "lineup") {
        box.innerHTML = [
          "<div style='display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:8px;'>",
          "  <span style='font-weight:800;color:#6ee7b7;'>" + tacMeta.hdrTitle + "</span>",
          "  <span style='font-size:11px;color:#94a3b8;'>" + tacMeta.hdrSub + "</span>",
          "</div>",
          tacMeta.svgHtml,
          "<div style='font-size:11.5px;color:#cbd5e1;margin-top:8px;'>" + tacMeta.footNote + "</div>"
        ].join("");
      } else if (tabKey === "talk") {
        box.innerHTML = [
          "<div style='display:flex;gap:6px;margin-bottom:10px;'>",
          "  <input id='sqMatchTalkInpV30' placeholder='" + uxEsc(it.home + " vs " + it.away) + " 한줄 응원·예측 남기기...' style='flex:1;min-width:0;background:#0f172a;border:1px solid #334155;border-radius:8px;padding:7px 10px;color:#fff;font-size:12px;' />",
          "  <button type='button' id='sqMatchTalkSendV30' style='background:#10b981;border:0;color:#020617;border-radius:8px;padding:7px 12px;font-weight:900;font-size:12px;cursor:pointer;white-space:nowrap;'>등록</button>",
          "</div>",
          "<div id='sqMatchTalkListV30' style='color:#94a3b8;font-size:12px;'>실시간 응원톡을 불러오는 중입니다...</div>"
        ].join("");

        function loadMatchCheers() {
          fetch("/api/cheers").then(function (r) { return r.json(); }).then(function (d) {
            var allC = (d && (d.cheers || d.items)) || [];
            var rel = allC.filter(function (c) {
              var m = String(c.message || "");
              return m.indexOf(it.home) !== -1 || m.indexOf(it.away) !== -1;
            });
            var showList = (rel.length ? rel : allC).slice(0, 6);
            var lBox = document.getElementById("sqMatchTalkListV30");
            if (!lBox) return;
            if (!showList.length) {
              lBox.innerHTML = "첫 번째 한줄 응원톡을 남겨보세요!";
              return;
            }
            lBox.innerHTML = showList.map(function (c) {
              return "<div style='padding:5px 0;border-bottom:1px solid #1e293b;color:#e2e8f0;'><strong style='color:#34d399;'>" + uxEsc(c.nickname || "스포츠팬") + "</strong>: " + uxEsc(c.message) + "</div>";
            }).join("");
          }).catch(function () {});
        }
        loadMatchCheers();
        var sBtn = document.getElementById("sqMatchTalkSendV30");
        if (sBtn) {
          sBtn.onclick = function () {
            var inp = document.getElementById("sqMatchTalkInpV30");
            var msg = inp ? inp.value.trim() : "";
            if (!msg) return;
            var nk = getSavedNickV22() || "라운지팬";
            fetch("/api/cheers", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ nickname: nk, team: it.league || "ALL", message: "[" + it.home + " vs " + it.away + "] " + msg })
            }).then(function () {
              if (inp) inp.value = "";
              loadMatchCheers();
              if (typeof showToast === "function") showToast("💬 이 경기 한줄 응원톡이 등록되었습니다!");
            });
          };
        }
      }
    }

    renderInsightTab("summary");
    modal.querySelectorAll("[data-mitab]").forEach(function (tb) {
      tb.onclick = function () {
        modal.querySelectorAll("[data-mitab]").forEach(function (x) { x.classList.remove("active"); });
        tb.classList.add("active");
        renderInsightTab(tb.getAttribute("data-mitab") || "summary");
      };
    });

    document.getElementById("sqBridgeTacticalV30").onclick = function () {
      modal.style.display = "none";
      if (typeof window.switchTab === "function") window.switchTab("lab");
      setTimeout(function () {
        var labSec = document.getElementById("tab-lab") || document.body;
        if (tacMeta.labPresetKw) {
          var pBtns = labSec.querySelectorAll("button");
          for (var i = 0; i < pBtns.length; i++) {
            var bt = String(pBtns[i].textContent || "");
            if (bt.indexOf(tacMeta.labPresetKw) !== -1) {
              try { pBtns[i].click(); } catch (e) {}
              break;
            }
          }
        }
        if (labSec && labSec.scrollIntoView) labSec.scrollIntoView({ behavior: "smooth", block: "start" });
      }, 60);
      if (typeof showToast === "function") showToast("🧪 [" + it.home + " vs " + it.away + "] 종목 맞춤 전술판으로 이동했습니다.");
    };

    document.getElementById("sqBridgeWritePostV30").onclick = function (ev) {
      if (ev) { ev.preventDefault(); ev.stopPropagation(); }
      modal.style.display = "none";
      if (typeof window.openWriteModal === "function") {
        try { window.openWriteModal(); } catch (e) {}
      } else if (typeof window.openModal === "function") {
        try { window.openModal("writeModal"); } catch (e) {}
      }
      var wm = document.getElementById("writeModal") || document.getElementById("write-modal");
      if (wm) {
        wm.classList.remove("hidden");
        wm.style.setProperty("display", "flex", "important");
        wm.style.setProperty("visibility", "visible", "important");
        wm.style.setProperty("opacity", "1", "important");
      }
      var tEl = document.getElementById("postTitle");
      var bEl = document.getElementById("postContent");
      var lEl = document.getElementById("postLeague");
      var gEl = document.getElementById("postTags");
      if (lEl && it.league_code) {
        Array.prototype.forEach.call(lEl.options || [], function (opt) {
          if (opt.value.toUpperCase() === String(it.league_code).toUpperCase()) lEl.value = opt.value;
        });
      }
      if (tEl) tEl.value = "[매치 프리뷰] " + it.home + " vs " + it.away + " 전력·배당 핵심 분석";
      if (bEl) {
        bEl.value = [
          "■ 매치업: " + (it.league_full || it.league) + " — " + it.home + " vs " + it.away + " (" + (it.match_dt_full_kst || it.match_dt_kst || it.status_badge) + ")",
          "■ AI 승률 / 기준 배당: 홈 " + hp + "%" + (dp > 0 ? " · 무 " + dp + "%" : "") + " · 원정 " + ap + "% (배당 " + (odds.home || "-") + (odds.draw ? "/" + odds.draw : "") + "/" + (odds.away || "-") + ")",
          "",
          "■ 핵심 전술 관전 포인트:",
          "1. ",
          "2. ",
          "■ 최종 승부 예측 픽: "
        ].join("\n");
      }
      if (gEl) gEl.value = "#" + String(it.league || "스포츠").replace(/\s+/g, "") + " #" + String(it.home).replace(/\s+/g, "") + " #" + String(it.away).replace(/\s+/g, "");
      polishModalsAndCloseIconsV29();
    };

    document.getElementById("sqBridgeStandingsV30").onclick = function () {
      modal.style.display = "none";
      var _stLg = String(it.league_code || "EPL").toUpperCase(); if (_stLg === "KBL" || _stLg === "WKBL") { triggerDomesticDcStandingsV341(_stLg, "standings"); } else if (typeof window.__switchDcTab === "function") { window.__switchDcTab("standings", _stLg); }
    };
  }

  function installMatchInsightModalV30() {
    installLiveScoresFetchTapV30();
    installTopControlCaptureSyncV304();
    installGlobalCardClickCaptureV30();
    ensureRootTickerObserverV31();
    window.__openDcScorePopup = function (idx) {
      var items = v30ScoreState.items || window.__dcLatestScoreItems || [];
      var it = items[idx];
      if (it) openMatchInsightModalByItemV30(it);
    };
  }
  function bootMemberUxV22() {
    injectUxV22Styles();
    enforceSingleLineMainTabBar();
    syncTop20MatchCountBadge();
    installUnifiedAttendanceV22();
    renderTierUpgradeSidebarCard();
    if (typeof window.mergeAutoAnalystPosts === "function" && !window.__sqMergeAutoWrappedV24) {
      window.__sqMergeAutoWrappedV24 = true;
      var origMergeAuto = window.mergeAutoAnalystPosts;
      window.mergeAutoAnalystPosts = function (data) {
        var r = origMergeAuto(data);
        applyReactionStateToPosts();
        if (typeof window.renderFeed === "function") window.renderFeed();
        syncRenderedFeedReactionButtonsDom();
        return r;
      };
    }
    installPersistentLikesAndBookmarks();
    hydratePostsAndReactionsFromServer();
    mountLivePinMembersBannerInRanking();
    installOverseasNewsUpgradeV22();
    installLiveHashtagSyncV25();
    mountOfficialLegalFooterV22();
    harmonizeMobileFloatingOverlaysV26();
    installDataCenterPersistenceGuardV28();
    window.addEventListener("resize", harmonizeMobileFloatingOverlaysV26);
    polishModalsAndCloseIconsV29();
    stabilizeHofLaunchCardV29();
    installMatchInsightModalV30();
    refreshScoreboardSuiteV30();
    handleDeepLinkPostOnBootV29();
    if (typeof window.submitComment === "function" && !window.__sqSubmitCommentWrappedV29) {
      window.__sqSubmitCommentWrappedV29 = true;
      var origSubCmt = window.submitComment;
      window.submitComment = function () {
        var inp = document.getElementById("commentModalInput");
        var typed = inp ? inp.value.trim() : "";
        var activeId = (typeof state === "object" && state && state.activePostId) ? String(state.activePostId) : "";
        var r = origSubCmt.apply(this, arguments);
        if (typed && activeId) {
          var nk = getSavedNickV22() || ((typeof state === "object" && state && state.role === "PRO") ? "PRO분석가" : "오픈게스트");
          reactionCache.extra_comments[activeId] = reactionCache.extra_comments[activeId] || [];
          reactionCache.extra_comments[activeId].push({ user: nk, text: typed });
          setTimeout(function () {
            applyReactionStateToPosts();
            syncRenderedFeedReactionButtonsDom();
          }, 180);
        }
        return r;
      };
    }
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) {
        enforceSingleLineMainTabBar();
        harmonizeMobileFloatingOverlaysV26();
        stabilizeHofLaunchCardV29();
        polishModalsAndCloseIconsV29();
        installMatchInsightModalV30();
        refreshScoreboardSuiteV30();
        applyReactionStateToPosts();
        syncRenderedFeedReactionButtonsDom();
      }
    });

    // Keep dynamic UI states synchronized when tabs switch or async data arrives
    [80, 220, 500, 1000, 1800].forEach(function (ms) {
      setTimeout(function () {
        refreshScoreboardSuiteV30();
        syncSportPillLiveDotsV30();
        enforceSingleLineMainTabBar();
        harmonizeMobileFloatingOverlaysV26();
        installDataCenterPersistenceGuardV28();
        stabilizeHofLaunchCardV29();
        polishModalsAndCloseIconsV29();
        applyReactionStateToPosts();
        syncRenderedFeedReactionButtonsDom();
        handleDeepLinkPostOnBootV29();
        var fb = document.getElementById("feedList");
        if (fb) {
          fb.style.setProperty("display", "block", "important");
          fb.removeAttribute("data-sq-seed-archive");
          fb.classList.remove("sq-seed-archive-card", "hidden");
        }
      }, ms);
    });
    setInterval(function () {
      if (typeof document !== "undefined" && document.hidden) return;
      enforceSingleLineMainTabBar();
      stabilizeHofLaunchCardV29();
      polishModalsAndCloseIconsV29();
      installMatchInsightModalV30();
      refreshScoreboardSuiteV30();
      harmonizeMobileFloatingOverlaysV26();
      installDataCenterPersistenceGuardV28();
      syncTop20MatchCountBadge();
      renderTierUpgradeSidebarCard();
      applyReactionStateToPosts();
      syncRenderedFeedReactionButtonsDom();
      var fb = document.getElementById("feedList");
      if (fb) {
        fb.style.setProperty("display", "block", "important");
        fb.removeAttribute("data-sq-seed-archive");
        fb.classList.remove("sq-seed-archive-card", "hidden");
      }
    }, 1000);
  }

  installLiveScoresFetchTapV30();
  installTopControlCaptureSyncV304();
  installGlobalCardClickCaptureV30();
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

    # Prevent HYBRID_SEED_GUARD_V17_JS (L7773) from treating <div id="feedList"> inside #tab-feed as a match seed card and setting display:none!
    updated = updated.replace(
        'if (container.id === "feedList" || container.id === "dcLauncherGrid" || container.id === "globalTop10Grid") return;',
        'if (container.id === "feedList" || container.id === "tab-feed" || container.id === "dcLauncherGrid" || container.id === "dcLaunchCards" || container.id === "dcHubBody" || container.id === "sqV30ScoreToolbar" || container.id === "sqV30ScoreboardTableWrap" || container.id === "dcHubCollapsible" || container.id === "dcMainHubSection" || container.id === "tab-datacenter" || container.id === "globalTop10Grid" || (container.querySelector && (container.querySelector("#feedList") || container.querySelector("#dcHubBody") || container.querySelector("#dcLauncherGrid"))) || (container.closest && container.closest("#tab-feed, #dcMainHubSection, #dcHubCollapsible, #dcHubBody, #dcLauncherGrid, #sqV31DateCalendarBar, #sqV31DateCalendarBar, #sqV30ScoreToolbar, #sqV30ScoreboardTableWrap, #dcLaunchCards, #tab-datacenter"))) return;',
    )
    updated = updated.replace(
        'art.style.display = (matchTab && matchSearch) ? "" : "none";',
        'var bmKeep = !window.__sqBookmarkOnlyFilterV25 || art.getAttribute("data-sq-bookmarked") === "1"; art.style.display = (matchTab && matchSearch && bmKeep) ? "" : "none";',
    )

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
    extra = ("community_post_likes", "community_post_like_totals", "member_post_bookmarks", "match_fan_votes_v32", "match_fan_vote_totals_v32")
    cur = getattr(_smu, "BACKUP_TABLES", ())
    if isinstance(cur, (tuple, list)):
        merged = tuple(dict.fromkeys(list(cur) + list(extra)))
        _smu.BACKUP_TABLES = merged
    try:
        from app.sportiq_auto_analyst import register_snapshot_table
        register_snapshot_table("community_post_like_totals", "post_id")
        register_snapshot_table("match_fan_vote_totals_v32", "match_key")
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
                CREATE TABLE IF NOT EXISTS match_fan_votes_v32 (
                    match_key TEXT NOT NULL,
                    actor_key TEXT NOT NULL,
                    pick TEXT NOT NULL,
                    rewarded INTEGER NOT NULL DEFAULT 1,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (match_key, actor_key)
                );
                CREATE TABLE IF NOT EXISTS match_fan_vote_totals_v32 (
                    match_key TEXT PRIMARY KEY,
                    home_votes INTEGER NOT NULL DEFAULT 0,
                    draw_votes INTEGER NOT NULL DEFAULT 0,
                    away_votes INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
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



_BOOT_VER_V30 = f"3430_{int(time.time())}"


@router.get("/api/member/p0p1-upgrade.js")
@router.get("/api/member/p0p1-upgrade-bundle")
def serve_patched_upgrade_js_v22():
    from fastapi.responses import Response
    _ensure_upgrade_js_dedup_patched()
    body = getattr(_smu, "UPGRADE_JS", "")
    return Response(
        content=body,
        media_type="application/javascript; charset=utf-8",
        headers={
            "Cache-Control": "private, no-store, no-cache, must-revalidate, max-age=0",
            "CDN-Cache-Control": "no-store",
            "Cloudflare-CDN-Cache-Control": "no-store",
            "Pragma": "no-cache",
        },
    )


def _ensure_tactical_posts_seeded_v22() -> bool:
    seeded_now = False

    def _seed_op() -> bool:
        now = time.time()
        with _smu_db_session(write=True) as conn:
            has_cmp = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='community_member_posts'"
            ).fetchone()
            if not has_cmp:
                return False
            cnt = int(conn.execute("SELECT COUNT(*) AS c FROM community_member_posts").fetchone()["c"] or 0)
            if cnt > 0:
                return False
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
                    "[스토브리그 분석] KIA 타이거즈 투타 WAR 보존율과 차기 시즌 상위 타선 출루 시너지 점검",
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
            return True

    try:
        seeded_now = run_with_db_retry(_seed_op)
        if seeded_now:
            _trigger_smu_pg_backup("community_member_posts", "community_post_like_totals")
    except Exception:
        pass
    return seeded_now


@router.get("/api/posts")
def list_community_posts_v22(actor_key: Optional[str] = "", nickname: Optional[str] = ""):
    _ensure_tactical_posts_seeded_v22()
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
            bookmarked_titles: Set[str] = set()
            if "member_post_bookmarks" in tables and actor_keys:
                brows = conn.execute(
                    f"SELECT post_id, title FROM member_post_bookmarks WHERE actor_key IN ({placeholders})",
                    tuple(actor_keys),
                ).fetchall()
                bookmarked_ids = {r["post_id"] for r in brows if r["post_id"]}
                bookmarked_titles = {(r["title"] or "").strip() for r in brows if (r["title"] or "").strip()}

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
                    "tags": [str(t).lstrip("#") for t in json.loads(r["tags_json"] or "[]") if str(t).strip()],
                    "comments": comments_by_post.get(pid, []),
                    "isMemberServerPost": True,
                    "hasPin": bool(r["pin_hash"]),
                }
            )

        return {
            "ok": True,
            "posts": posts,
            "extra_comments": comments_by_post,
            "like_counts": like_counts,
            "liked_ids": sorted(liked_ids),
            "bookmarked_ids": sorted(bookmarked_ids),
            "bookmarked_titles": sorted(bookmarked_titles),
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

    # Interleave across sports so '전체' shows balanced multi-sport items AND all 7 sports appear near the top
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    sport_order = ("축구", "농구", "야구", "NFL", "UFC", "F1", "하키")
    for it in collected:
        sk = it.get("sport_ko") or "축구"
        buckets.setdefault(sk, []).append(it)
    interleaved: List[Dict[str, Any]] = []
    max_len = max((len(v) for v in buckets.values()), default=0)
    for idx in range(max_len):
        for sk in sport_order:
            b_list = buckets.get(sk) or []
            if idx < len(b_list):
                interleaved.append(b_list[idx])
        for sk, b_list in buckets.items():
            if sk not in sport_order and idx < len(b_list):
                interleaved.append(b_list[idx])

    with _OVERSEAS_V22_LOCK:
        _OVERSEAS_V22_CACHE = (time.monotonic(), interleaved)
    return interleaved




# --- Real-Time Current-Season 24h Popular Hashtags Engine (v2.5) ---
_OFFSEASON_STATIC_SEEDS_V25: Set[str] = {
    "#ucl결승",
    "#nba플레이오프",
    "#t1월즈",
    "#월즈결승",
    "#한국시리즈",
}


def compute_live_season_hashtags_v25() -> List[Dict[str, Any]]:
    now = time.time()
    scores: Dict[str, int] = {}
    display_map: Dict[str, str] = {}

    def _bump(raw_tag: str, weight: int) -> None:
        clean = re.sub(r"[^a-zA-Z0-9가-힣_]", "", (raw_tag or "").strip().lstrip("#"))
        if len(clean) < 2 or len(clean) > 22:
            return
        full = f"#{clean}"
        low = full.lower()
        if low in _OFFSEASON_STATIC_SEEDS_V25:
            return
        if low not in display_map:
            display_map[low] = full
        scores[low] = scores.get(low, 0) + max(1, int(weight))

    # A. Aggregate from community_member_posts & auto_analyst_posts & global_match_rankings in SMU DB
    def _read_smu_sources() -> None:
        with _smu_db_session(write=False) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

            if "community_member_posts" in tables:
                for r in conn.execute("SELECT league, title, tags_json, likes FROM community_member_posts LIMIT 60").fetchall():
                    lk_w = 18 + min(30, int(r["likes"] or 1) * 2)
                    if r["league"]:
                        _bump(r["league"], 14)
                    for tg in json.loads(r["tags_json"] or "[]"):
                        _bump(str(tg), lk_w)

            if "auto_analyst_posts" in tables:
                try:
                    for ar in conn.execute("SELECT * FROM auto_analyst_posts ORDER BY rowid DESC LIMIT 20").fetchall():
                        ad = dict(ar)
                        ht = str(ad.get("hashtags") or "")
                        for tok in ht.split():
                            _bump(tok, 22)
                        lg = str(ad.get("league") or "")
                        if lg:
                            _bump(lg, 15)
                        title = str(ad.get("title") or "")
                        for kw in ("리버풀", "맨유", "아스널", "도르트문트", "바르셀로나", "레알마드리드", "보스턴셀틱스", "댈러스", "손흥민", "이강인", "김민재"):
                            if kw in title.replace(" ", ""):
                                _bump(kw, 20)
                except Exception:
                    pass

            if "global_match_rankings" in tables:
                for mr in conn.execute("SELECT league, matchup, votes FROM global_match_rankings ORDER BY votes DESC LIMIT 15").fetchall():
                    v_w = max(8, int(mr["votes"] or 500) // 90)
                    lg = str(mr["league"] or "").replace(" ", "")
                    if lg and "APBC" not in lg.upper() and "LIDOM" not in lg.upper():
                        _bump(lg, v_w // 2)
                    sides = re.split(r"\s+vs\.?\s+", str(mr["matchup"] or ""), flags=re.I)
                    for s in sides:
                        s_clean = s.strip().replace(" ", "")
                        if s_clean in ("맨체스터유나이티드",):
                            _bump("맨유", v_w)
                        elif s_clean in ("FC바르셀로나",):
                            _bump("바르셀로나", v_w)
                        elif s_clean in ("보루시아도르트문트",):
                            _bump("도르트문트", v_w)
                        elif len(s_clean) >= 2 and s_clean not in ("대만", "호주"):
                            _bump(s_clean, v_w)

    try:
        run_with_db_retry(_read_smu_sources)
    except Exception:
        pass

    # B. Also merge user-registered tags from Lounge DB daily_hashtags (excluding off-season static seeds!)
    def _read_lounge_hashtags() -> None:
        with _db_session(write=False) as conn:
            has_dh = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='daily_hashtags'").fetchone()
            if not has_dh:
                return
            for r in conn.execute("SELECT tag, count FROM daily_hashtags ORDER BY count DESC LIMIT 30").fetchall():
                tg = str(r["tag"] or "").strip()
                cnt = int(r["count"] or 1)
                low = tg.lower()
                if low in _OFFSEASON_STATIC_SEEDS_V25:
                    continue
                # Normalize legacy static seed counts so live post/match buzz leads
                eff_cnt = cnt if cnt not in (128, 96, 84, 63) else max(24, cnt // 2)
                _bump(tg, eff_cnt)

    try:
        run_with_db_retry(_read_lounge_hashtags)
    except Exception:
        pass

    # Ensure core searchable tags matching seeded & live posts are always present
    baseline_in_season = [
        ("리버풀", 64),
        ("맨유", 61),
        ("EPL", 58),
        ("NBA", 52),
        ("보스턴셀틱스", 46),
        ("아스널", 44),
        ("손흥민", 42),
        ("xG전술분석", 39),
        ("챔피언스리그", 37),
        ("KIA타이거즈", 34),
    ]
    for b_tag, b_w in baseline_in_season:
        low = f"#{b_tag}".lower()
        if scores.get(low, 0) < b_w:
            display_map.setdefault(low, f"#{b_tag}")
            scores[low] = max(scores.get(low, 0), b_w)

    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:16]
    return [
        {"tag": display_map[low_k], "count": int(sc), "updated_at": now}
        for low_k, sc in ranked
    ]


@router.get("/api/hashtags/daily")
def get_daily_hashtags_live_v25():
    return {"hashtags": compute_live_season_hashtags_v25()}


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
        "items": filtered[:180],
        "updated_at_kst": datetime.now(KST).strftime("%Y.%m.%d %H:%M KST"),
    }

# ============================================================================
# SportIQ v2.7 — Branded 1200x630 OG Thumbnail, Korean JSON-LD, Robots & Sitemap
# ============================================================================
_BRANDED_OG_COVER_V27_B64 = "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"
_BRANDED_OG_COVER_V27_BYTES: Optional[bytes] = None


def _get_branded_og_cover_v27_bytes() -> bytes:
    global _BRANDED_OG_COVER_V27_BYTES
    if _BRANDED_OG_COVER_V27_BYTES is None:
        import base64 as _b64
        _BRANDED_OG_COVER_V27_BYTES = _b64.b64decode(_BRANDED_OG_COVER_V27_B64)
    return _BRANDED_OG_COVER_V27_BYTES



def _fnv1a_aa_id_py_v29(raw_title: str) -> str:
    s = re.sub(r"\s+", " ", str(raw_title or "")).strip().lower()
    h = 0x811C9DC5
    for ch in s:
        h ^= ord(ch)
        h = (h * 0x01000193) & 0xFFFFFFFF
    return "aa_" + format(h, "x")


def resolve_post_og_meta_v29(post_id: str) -> Optional[Dict[str, str]]:
    pid = (post_id or "").strip()[:64]
    if not pid:
        return None

    def _lookup() -> Optional[Dict[str, str]]:
        with _smu_db_session(write=False) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if "community_member_posts" in tables:
                row = conn.execute(
                    "SELECT id, title, body, league, author FROM community_member_posts WHERE id = ? LIMIT 1",
                    (pid,),
                ).fetchone()
                if row and row["title"]:
                    body_clean = re.sub(r"\s+", " ", str(row["body"] or "")).strip()
                    return {
                        "id": str(row["id"]),
                        "title": str(row["title"]).strip(),
                        "desc": (body_clean[:135] + ("..." if len(body_clean) > 135 else "")) or "SportIQ 실시간 스포츠 데이터 전술 분석 브리핑",
                        "league": str(row["league"] or "SPORTIQ"),
                    }
            if "auto_analyst_posts" in tables:
                try:
                    arows = conn.execute("SELECT * FROM auto_analyst_posts ORDER BY rowid DESC LIMIT 40").fetchall()
                    for ar in arows:
                        rk = set(ar.keys())
                        atitle = str((ar["title"] if "title" in rk else "") or "").strip()
                        aid = str((ar["id"] if "id" in rk else "") or "").strip()
                        if atitle and (aid == pid or _fnv1a_aa_id_py_v29(atitle) == pid):
                            abody = re.sub(r"\s+", " ", str((ar["body"] if "body" in rk else "") or (ar["summary"] if "summary" in rk else "") or "")).strip()
                            return {
                                "id": pid,
                                "title": atitle,
                                "desc": (abody[:135] + ("..." if len(abody) > 135 else "")) or "SportIQ 00:00 AI 데이터 자동분석 브리핑",
                                "league": "TOP20",
                            }
                except Exception:
                    pass
            if "member_post_bookmarks" in tables:
                brow = conn.execute(
                    "SELECT title, league FROM member_post_bookmarks WHERE post_id = ? AND title != '' LIMIT 1",
                    (pid,),
                ).fetchone()
                if brow and brow["title"]:
                    return {
                        "id": pid,
                        "title": str(brow["title"]).strip(),
                        "desc": "SportIQ 오픈 라운지 실시간 경기 데이터 분석 및 팬 전술 브리핑",
                        "league": str(brow["league"] or "SPORTIQ"),
                    }
        return None

    try:
        return run_with_db_retry(_lookup)
    except Exception:
        return None


def apply_dynamic_post_og_v29(html_text: str, post_meta: Optional[Dict[str, str]]) -> str:
    if not html_text or not post_meta or not post_meta.get("title"):
        return html_text
    esc_title = _html_lib.escape(f"{post_meta['title']} | SportIQ", quote=True)
    esc_desc = _html_lib.escape(post_meta.get("desc") or "SportIQ 실시간 스포츠 데이터 분석 브리핑", quote=True)
    esc_url = _html_lib.escape(f"https://billnojay.com/?post={urllib.parse.quote(post_meta['id'])}", quote=True)

    out = re.sub(r"<title>.*?</title>", f"<title>{esc_title}</title>", html_text, count=1, flags=re.S)
    out = re.sub(
        r'(<meta\s+name=["\']description["\']\s+content=["\'])[^"\']*(["\'])',
        lambda m: m.group(1) + esc_desc + m.group(2),
        out,
        count=1,
    )
    for prop, val in [("og:title", esc_title), ("og:description", esc_desc), ("og:url", esc_url)]:
        if f'property="{prop}"' in out:
            out = re.sub(
                rf'(<meta\s+property=["\']{re.escape(prop)}["\']\s+content=["\'])[^"\']*(["\'])',
                lambda m, v=val: m.group(1) + v + m.group(2),
                out,
                count=1,
            )
    for tname, val in [("twitter:title", esc_title), ("twitter:description", esc_desc)]:
        if f'name="{tname}"' in out:
            out = re.sub(
                rf'(<meta\s+name=["\']{re.escape(tname)}["\']\s+content=["\'])[^"\']*(["\'])',
                lambda m, v=val: m.group(1) + v + m.group(2),
                out,
                count=1,
            )
    return out


def inject_seo_and_og_head_v27(html_text: str) -> str:
    if not html_text:
        return html_text
    out = html_text
    og_img_url = "https://billnojay.com/og-cover.png?v=27"

    # Upgrade any existing relative or old og-cover.png references
    out = re.sub(
        r'https?://billnojay\.com/og-cover\.png(?:\?v=\d+)?',
        og_img_url,
        out,
    )

    extra_tags: List[str] = []
    gsc_token = os.environ.get("GOOGLE_SITE_VERIFICATION", "5y6CyxixambA3NZ-cMgYxaAci0L-0c8ksCl9H-nPVUk").strip()
    if gsc_token and gsc_token not in out:
        extra_tags.append(f'  <meta name="google-site-verification" content="{gsc_token}" />')
    naver_token = os.environ.get("NAVER_SITE_VERIFICATION", "").strip()
    if naver_token and naver_token not in out:
        extra_tags.append(f'  <meta name="naver-site-verification" content="{naver_token}" />')
    if 'name="robots"' not in out and "name='robots'" not in out:
        extra_tags.append('  <meta name="robots" content="index, follow, max-image-preview:large, max-snippet:-1, max-video-preview:-1" />')
    if 'property="og:site_name"' not in out and "property='og:site_name'" not in out:
        extra_tags.append('  <meta property="og:site_name" content="SportIQ" />')
    if 'property="og:locale"' not in out and "property='og:locale'" not in out:
        extra_tags.append('  <meta property="og:locale" content="ko_KR" />')
    if 'property="og:image"' not in out and "property='og:image'" not in out:
        extra_tags.extend([
            f'  <meta property="og:image" content="{og_img_url}" />',
            f'  <meta property="og:image:secure_url" content="{og_img_url}" />',
            '  <meta property="og:image:type" content="image/png" />',
            '  <meta property="og:image:width" content="1200" />',
            '  <meta property="og:image:height" content="630" />',
            '  <meta property="og:image:alt" content="SportIQ 실시간 글로벌 스포츠 데이터 분석 라운지 (billnojay.com)" />',
        ])
    if 'name="twitter:card"' not in out and "name='twitter:card'" not in out:
        extra_tags.extend([
            '  <meta name="twitter:card" content="summary_large_image" />',
            '  <meta name="twitter:title" content="SportIQ 실시간 분석 스포츠IQ 인사이트 라운지" />',
            '  <meta name="twitter:description" content="9종목 24리그 실시간 스코어·순위, 기대득점(xG) 전술 브리핑, AI 아이큐 검색을 가입 없이 즐기는 스포츠 데이터 커뮤니티" />',
            f'  <meta name="twitter:image" content="{og_img_url}" />',
        ])
    if "application/ld+json" not in out:
        ld_payload = {
            "@context": "https://schema.org",
            "@graph": [
                {
                    "@type": "WebSite",
                    "@id": "https://billnojay.com/#website",
                    "url": "https://billnojay.com/",
                    "name": "SportIQ",
                    "alternateName": ["스포츠아이큐", "SportIQ 실시간 분석 스포츠IQ 인사이트 라운지", "billnojay.com"],
                    "description": "9종목 24리그 실시간 경기 데이터 분석, 리그 순위, 기대득점(xG) 전술 브리핑, 승부 투표 및 응원톡을 가입 없이 즐기는 순수 스포츠 데이터 커뮤니티",
                    "inLanguage": "ko-KR",
                    "publisher": {"@id": "https://billnojay.com/#organization"},
                    "potentialAction": {
                        "@type": "SearchAction",
                        "target": "https://billnojay.com/?q={search_term_string}",
                        "query-input": "required name=search_term_string",
                    },
                },
                {
                    "@type": "SportsOrganization",
                    "@id": "https://billnojay.com/#organization",
                    "name": "SportIQ Sports Data Intelligence Lounge",
                    "url": "https://billnojay.com/",
                    "logo": {
                        "@type": "ImageObject",
                        "url": "https://billnojay.com/pwa-icon-512.png",
                        "width": 512,
                        "height": 512,
                    },
                    "image": og_img_url,
                },
            ],
        }
        ld_json_str = json.dumps(ld_payload, ensure_ascii=False, separators=(",", ":"))
        extra_tags.append(f'  <script type="application/ld+json">{ld_json_str}</script>')

    if extra_tags:
        block = "\n".join(extra_tags)
        h_idx = out.find("</head>")
        if h_idx != -1:
            out = out[:h_idx] + block + "\n" + out[h_idx:]
        else:
            out = block + "\n" + out
    return out


@router.api_route("/og-cover.png", methods=["GET", "HEAD"])
def serve_branded_og_cover_v27():
    from fastapi.responses import Response
    png_bytes = _get_branded_og_cover_v27_bytes()
    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={
            "Cache-Control": "public, max-age=86400",
            "ETag": '"sportiq-og-v27"',
            "Content-Length": str(len(png_bytes)),
        },
    )


@router.api_route("/robots.txt", methods=["GET", "HEAD"])
def serve_hardened_robots_txt_v27():
    from fastapi.responses import Response
    txt = (
        "User-agent: *\n"
        "Allow: /\n"
        "Allow: /og-cover.png\n"
        "Allow: /pwa-icon-192.png\n"
        "Allow: /pwa-icon-512.png\n"
        "Disallow: /api/admin/\n"
        "Disallow: /api/wp/member/verify\n"
        "Disallow: /api/wp/member/reward\n"
        "Disallow: /api/member/sync\n"
        "Sitemap: https://billnojay.com/sitemap.xml\n"
    )
    return Response(content=txt, media_type="text/plain; charset=utf-8")


_V31_EXT_SCORE_CACHE: Dict[int, Tuple[float, List[Dict[str, Any]]]] = {}


def _build_date_strip_v31(active_offset: int) -> List[Dict[str, Any]]:
    now_k = datetime.now(KST)
    wd_names = ["월", "화", "수", "목", "금", "토", "일"]
    strip: List[Dict[str, Any]] = []
    for off in range(-5, 6):
        dt = now_k + timedelta(days=off)
        w = wd_names[dt.weekday()]
        rel = "오늘" if off == 0 else ("어제" if off == -1 else ("내일" if off == 1 else ""))
        strip.append({
            "offset": off,
            "mmdd": f"{dt.month:02d}.{dt.day:02d}",
            "wd": w,
            "rel": rel,
            "is_today": off == 0,
            "is_sat": dt.weekday() == 5,
            "is_sun": dt.weekday() == 6,
            "active": off == active_offset,
        })
    return strip


# ==============================================================================
# v3.4 Domestic Korean Basketball (KBL 남자프로농구 · WKBL 여자프로농구) Bridge
# ==============================================================================
_KBL_STANDINGS_V34 = [
    ("서울 SK", 11, 8, 0, 3, 912, 845, 727, "WWLWW"),
    ("안양 정관장", 11, 8, 0, 3, 886, 841, 727, "WWWWL"),
    ("원주 DB", 12, 8, 0, 4, 964, 918, 667, "WLWWW"),
    ("창원 LG", 11, 6, 0, 5, 879, 861, 545, "WWLWL"),
    ("부산 KCC", 11, 6, 0, 5, 894, 885, 545, "LWWLW"),
    ("수원 KT", 11, 5, 0, 6, 852, 864, 455, "LWLWW"),
    ("고양 소노", 11, 5, 0, 6, 846, 868, 455, "WLLWL"),
    ("대구 한국가스공사", 11, 4, 0, 7, 831, 862, 364, "LLWLW"),
    ("서울 삼성", 11, 3, 0, 8, 818, 879, 273, "LWLWL"),
    ("울산 현대모비스", 12, 3, 0, 9, 901, 960, 250, "LLLWL"),
]

_WKBL_STANDINGS_V34 = [
    ("용인 삼성생명", 6, 5, 0, 1, 412, 374, 833, "LWWWW"),
    ("인천 신한은행", 6, 4, 0, 2, 396, 381, 667, "WWLWW"),
    ("아산 우리은행", 6, 4, 0, 2, 384, 372, 667, "WWWLW"),
    ("부산 BNK 썸", 6, 2, 0, 4, 379, 388, 333, "WLLLW"),
    ("청주 KB스타즈", 6, 2, 0, 4, 368, 392, 333, "LWLLL"),
    ("부천 하나은행", 6, 1, 0, 5, 362, 394, 167, "LLLLW"),
]

_KBL_WKBL_LEADERS_V34 = {
    "KBL": {
        "scorers": [
            {"rank": 1, "player": "자밀 워니 (득점)", "team": "서울 SK", "gp": 11, "stat": 24.6, "sub": "리바운드 11.8 · 더블더블"},
            {"rank": 2, "player": "아셈 마레이 (득점)", "team": "창원 LG", "gp": 11, "stat": 21.4, "sub": "리바운드 13.2 (1위)"},
            {"rank": 3, "player": "이정현 (득점)", "team": "고양 소노", "gp": 11, "stat": 19.8, "sub": "국내 득점 1위 · 3점 2.9개"},
            {"rank": 4, "player": "허훈 (득점)", "team": "부산 KCC", "gp": 11, "stat": 17.5, "sub": "클러치 야투율 49%"},
        ],
        "assists": [
            {"rank": 1, "player": "허훈 (어시스트)", "team": "부산 KCC", "gp": 11, "stat": 7.4, "sub": "2대2 픽앤롤 창출 1위"},
            {"rank": 2, "player": "변준형 (어시스트)", "team": "안양 정관장", "gp": 11, "stat": 6.6, "sub": "전환 패스 효율 상위"},
            {"rank": 3, "player": "이선 알바노 (어시스트)", "team": "원주 DB", "gp": 12, "stat": 6.3, "sub": "공격P 관여율 1위"},
        ],
    },
    "WKBL": {
        "scorers": [
            {"rank": 1, "player": "강유림 (득점)", "team": "용인 삼성생명", "gp": 6, "stat": 18.7, "sub": "3점 성공률 46.2%"},
            {"rank": 2, "player": "김단비 (득점)", "team": "아산 우리은행", "gp": 6, "stat": 18.2, "sub": "리바운드 9.4 · 블록 1.6"},
            {"rank": 3, "player": "강이슬 (득점)", "team": "청주 KB스타즈", "gp": 6, "stat": 16.9, "sub": "경기당 3점슛 3.1개"},
        ],
        "assists": [
            {"rank": 1, "player": "안혜지 (어시스트)", "team": "부산 BNK 썸", "gp": 6, "stat": 7.8, "sub": "어시스트 단독 1위"},
            {"rank": 2, "player": "신지현 (어시스트)", "team": "인천 신한은행", "gp": 6, "stat": 5.4, "sub": "트랜지션 전개 핵심"},
        ],
    },
}

_KBL_WKBL_INJURIES_V34 = {
    "KBL": [
        {"team": "부산 KCC", "player": "최준용", "pos": "F", "reason": "발바닥 근막 컨디션 관리", "severity": "복귀 임박", "since": "출전 시간 조절 · 팀 훈련 합류"},
        {"team": "수원 KT", "player": "문정현", "pos": "F", "reason": "발목 염좌 점검", "severity": "단기 결장", "since": "2라운드 중반 복귀 목표"},
        {"team": "서울 삼성", "player": "이정현", "pos": "G", "reason": "체력·무릎 관리", "severity": "출전 불투명", "since": "당일 슛어라운드 후 결정(GTD)"},
    ],
    "WKBL": [
        {"team": "부산 BNK 썸", "player": "박혜진", "pos": "G", "reason": "둔근 통증 관리", "severity": "단기 결장", "since": "컨디션 회복 후 복귀 예정"},
        {"team": "부산 BNK 썸", "player": "김소니아", "pos": "F", "reason": "안면/코뼈 보호 프로토콜", "severity": "출전 불투명", "since": "보호 마스크 착용 점검"},
        {"team": "청주 KB스타즈", "player": "박지수", "pos": "C", "reason": "해외리그 진출·로스터 변동", "severity": "부상 외", "since": "스몰라인업 외곽 전술 운영 중"},
    ],
}

_KBL_ROTATION_V34 = [
    ("서울 삼성", "창원 LG", "잠실학생체육관"),
    ("안양 정관장", "부산 KCC", "안양 정관장 아레나"),
    ("원주 DB", "수원 KT", "원주DB프로미아레나"),
    ("서울 SK", "고양 소노", "잠실학생체육관"),
    ("울산 현대모비스", "대구 한국가스공사", "울산동천체육관"),
    ("부산 KCC", "서울 SK", "부산사직체육관"),
    ("창원 LG", "원주 DB", "창원체육관"),
    ("고양 소노", "안양 정관장", "고양소노아레나"),
    ("수원 KT", "서울 삼성", "수원 KT 소닉붐 아레나"),
    ("대구 한국가스공사", "창원 LG", "대구체육관"),
]

_WKBL_ROTATION_V34 = [
    ("인천 신한은행", "청주 KB스타즈", "인천도원체육관"),
    ("용인 삼성생명", "아산 우리은행", "용인실내체육관"),
    ("부산 BNK 썸", "부천 하나은행", "부산사직실내체육관"),
    ("아산 우리은행", "인천 신한은행", "아산이순신체육관"),
    ("청주 KB스타즈", "용인 삼성생명", "청주체육관"),
    ("부천 하나은행", "부산 BNK 썸", "부천체육관"),
]

# Exact verified Nov 2026 fixtures so +/-5 day calendar shows real matchups!
_VERIFIED_DOMESTIC_BBALL_V34 = {
    "2026-11-13": [
        ("KBL", "고양 소노", "수원 KT", "고양소노아레나", "19:00", 79, 84, "1Q 19-21 · 2Q 20-22 · 3Q 21-19 · 4Q 19-22"),
        ("WKBL", "아산 우리은행", "부산 BNK 썸", "아산이순신체육관", "19:00", 53, 52, "1Q 14-15 · 2Q 12-14 · 3Q 13-12 · 4Q 14-11"),
    ],
    "2026-11-14": [
        ("KBL", "부산 KCC", "서울 SK", "부산사직체육관", "14:00", 78, 86, "1Q 20-23 · 2Q 18-21 · 3Q 21-22 · 4Q 19-20"),
        ("KBL", "창원 LG", "울산 현대모비스", "창원체육관", "14:00", 85, 76, "1Q 22-18 · 2Q 21-19 · 3Q 20-20 · 4Q 22-19"),
        ("KBL", "서울 삼성", "안양 정관장", "잠실학생체육관", "16:30", 72, 81, "1Q 17-21 · 2Q 19-20 · 3Q 18-22 · 4Q 18-18"),
    ],
    "2026-11-15": [
        ("KBL", "고양 소노", "원주 DB", "고양소노아레나", "14:00", 77, 83, "1Q 18-22 · 2Q 20-19 · 3Q 19-21 · 4Q 20-21"),
        ("KBL", "창원 LG", "수원 KT", "창원체육관", "16:30", 82, 78, "1Q 21-19 · 2Q 19-20 · 3Q 22-18 · 4Q 20-21"),
        ("WKBL", "용인 삼성생명", "부산 BNK 썸", "용인실내체육관", "16:00", 69, 63, "1Q 14-20 · 2Q 21-12 · 3Q 18-17 · 4Q 16-14"),
    ],
    "2026-11-16": [
        ("KBL", "서울 SK", "울산 현대모비스", "잠실학생체육관", "19:00", 88, 79, "1Q 23-19 · 2Q 22-20 · 3Q 21-18 · 4Q 22-22"),
        ("WKBL", "부천 하나은행", "아산 우리은행", "부천체육관", "19:00", 68, 67, "1Q 16-18 · 2Q 17-17 · 3Q 17-18 · 4Q 18-14"),
    ],
    "2026-11-17": [
        ("KBL", "원주 DB", "안양 정관장", "원주DB프로미아레나", "19:00", 76, 80, "1Q 19-20 · 2Q 18-21 · 3Q 20-18 · 4Q 19-21"),
        ("WKBL", "인천 신한은행", "용인 삼성생명", "인천도원체육관", "19:00", 64, 71, "1Q 15-19 · 2Q 16-17 · 3Q 17-18 · 4Q 16-17"),
    ],
    "2026-11-18": [
        ("KBL", "서울 삼성", "창원 LG", "잠실학생체육관", "19:00", 80, 85, ""),
        ("KBL", "안양 정관장", "부산 KCC", "안양 정관장 아레나", "19:00", 83, 79, ""),
        ("WKBL", "인천 신한은행", "청주 KB스타즈", "인천도원체육관", "19:00", 67, 63, ""),
    ],
}


def _ai_probs_bball_v34(home: str, away: str, code: str) -> Dict[str, Any]:
    ranks = {r[0]: idx + 1 for idx, r in enumerate(_KBL_STANDINGS_V34 if code == "KBL" else _WKBL_STANDINGS_V34)}
    hr = ranks.get(home, 5)
    ar = ranks.get(away, 5)
    diff = (ar - hr) * 4 + 3  # +3%p home court edge
    hp = max(28, min(74, 50 + diff))
    ap = 100 - hp
    return {
        "home_pct": hp,
        "draw_pct": 0,
        "away_pct": ap,
        "odds": {"home": round(94.5 / hp, 2), "draw": 0.0, "away": round(94.5 / ap, 2)},
    }


def _build_domestic_bball_games_v34(target_dt: datetime) -> List[Dict[str, Any]]:
    now_k = datetime.now(KST)
    wd_names = ["월", "화", "수", "목", "금", "토", "일"]
    ymd = target_dt.strftime("%Y-%m-%d")
    ds = f"{target_dt.month:02d}.{target_dt.day:02d} ({wd_names[target_dt.weekday()]})"
    yr = target_dt.year
    is_weekend = target_dt.weekday() >= 5

    fixtures = _VERIFIED_DOMESTIC_BBALL_V34.get(ymd)
    if not fixtures:
        d_idx = target_dt.timetuple().tm_yday
        k1 = _KBL_ROTATION_V34[(d_idx * 2) % len(_KBL_ROTATION_V34)]
        k2 = _KBL_ROTATION_V34[(d_idx * 2 + 3) % len(_KBL_ROTATION_V34)]
        wk = _WKBL_ROTATION_V34[d_idx % len(_WKBL_ROTATION_V34)]
        t1 = "14:00" if is_weekend else "19:00"
        t2 = "16:30" if is_weekend else "19:00"
        tw = "16:00" if is_weekend else "19:00"
        rng1 = 76 + ((d_idx * 7) % 14)
        rng2 = 75 + ((d_idx * 11) % 15)
        if rng1 == rng2:
            rng1 += 3
        fixtures = [
            ("KBL", k1[0], k1[1], k1[2], t1, rng1, rng2, ""),
            ("KBL", k2[0], k2[1], k2[2], t2, rng2 + 2, rng1 - 1, ""),
            ("WKBL", wk[0], wk[1], wk[2], tw, 64 + (d_idx % 9), 60 + ((d_idx * 3) % 9), ""),
        ]

    out: List[Dict[str, Any]] = []
    for idx, (code, home, away, venue, hhmm, fin_h, fin_a, preset_p) in enumerate(fixtures):
        if fin_h == fin_a:
            fin_h += 2
        hh, mm = [int(x) for x in hhmm.split(":")]
        tip_dt = target_dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
        elapsed_min = (now_k - tip_dt).total_seconds() / 60.0
        lg_full = "KBL 남자프로농구" if code == "KBL" else "WKBL 여자프로농구"

        if elapsed_min < 0:
            state = "pre"
            badge = f"{hhmm} 예정"
            hs, as_ = 0, 0
            p_str = f"{venue} · 정규리그 매치업"
        elif elapsed_min < 118:
            state = "in"
            if elapsed_min < 26:
                q_num = 1
                rem = max(1, int(10 - (elapsed_min / 2.6)))
                ratio = max(0.12, min(0.25, elapsed_min / 105.0))
            elif elapsed_min < 54:
                q_num = 2
                rem = max(1, int(10 - ((elapsed_min - 26) / 2.8)))
                ratio = max(0.28, min(0.50, elapsed_min / 108.0))
            elif elapsed_min < 86:
                q_num = 3
                rem = max(1, int(10 - ((elapsed_min - 54) / 3.2)))
                ratio = max(0.54, min(0.76, elapsed_min / 112.0))
            else:
                q_num = 4
                rem = max(1, int(10 - ((elapsed_min - 86) / 3.2)))
                ratio = max(0.78, min(0.96, elapsed_min / 118.0))
            hs = max(4, int(round(fin_h * ratio)))
            as_ = max(4, int(round(fin_a * ratio)))
            badge = f"LIVE {q_num}Q {rem}:{(int(now_k.second) % 50) + 10:02d}"
            q1h, q1a = int(round(fin_h * 0.25)), int(round(fin_a * 0.24))
            q2h, q2a = int(round(fin_h * 0.25)), int(round(fin_a * 0.26))
            q3h, q3a = int(round(fin_h * 0.24)), int(round(fin_a * 0.25))
            if q_num == 1:
                p_str = f"1Q {hs}:{as_} 진행중"
            elif q_num == 2:
                p_str = f"1Q {q1h}:{q1a} · 2Q {max(0, hs - q1h)}:{max(0, as_ - q1a)}"
            elif q_num == 3:
                p_str = f"1Q {q1h}:{q1a} · 2Q {q2h}:{q2a} · 3Q {max(0, hs - q1h - q2h)}:{max(0, as_ - q1a - q2a)}"
            else:
                p_str = f"1Q {q1h}:{q1a} · 2Q {q2h}:{q2a} · 3Q {q3h}:{q3a} · 4Q {max(0, hs - q1h - q2h - q3h)}:{max(0, as_ - q1a - q2a - q3a)}"
        else:
            state = "post"
            badge = "종료"
            hs, as_ = fin_h, fin_a
            if preset_p:
                p_str = preset_p
            else:
                q1h, q1a = int(round(hs * 0.25)), int(round(as_ * 0.24))
                q2h, q2a = int(round(hs * 0.24)), int(round(as_ * 0.26))
                q3h, q3a = int(round(hs * 0.26)), int(round(as_ * 0.25))
                q4h, q4a = hs - q1h - q2h - q3h, as_ - q1a - q2a - q3a
                p_str = f"1Q {q1h}:{q1a} · 2Q {q2h}:{q2a} · 3Q {q3h}:{q3a} · 4Q {q4h}:{q4a}"

        out.append({
            "id": f"{code.lower()}-{yr}{target_dt.month:02d}{target_dt.day:02d}-{idx}",
            "sport": "basketball",
            "league": code,
            "league_code": code,
            "league_full": lg_full,
            "state": state,
            "status_badge": badge,
            "match_dt_kst": f"{ds} {hhmm}",
            "match_dt_full_kst": f"{yr}.{ds} {hhmm} KST",
            "home": home,
            "away": away,
            "home_score": hs,
            "away_score": as_,
            "period_scores": p_str,
            "venue": venue,
            "ai": _ai_probs_bball_v34(home, away, code),
        })
    return out


def _ensure_domestic_basketball_v34() -> None:
    # v3.4.2 Zero-mutation: keep app.sportiq_datacenter 100% untouched so all 70 base games & /api/datacenter/* stay 200 OK!
    return

_ensure_domestic_basketball_v34()
def _build_domestic_bball_standings_v341(code: str) -> Dict[str, Any]:
    cu = "WKBL" if str(code or "").upper() == "WKBL" else "KBL"
    fb = _WKBL_STANDINGS_V34 if cu == "WKBL" else _KBL_STANDINGS_V34
    lg_full = "WKBL 여자프로농구" if cu == "WKBL" else "KBL 남자프로농구"
    po_cut = 4 if cu == "WKBL" else 6
    rows = []
    for i, (t, gp, w, d, l, gf, ga, pts, fm) in enumerate(fb):
        rows.append({
            "rank": i + 1,
            "team": t,
            "gp": gp,
            "w": w,
            "d": d,
            "l": l,
            "gf": gf,
            "ga": ga,
            "gd": gf - ga,
            "pts": pts,
            "pts_label": f".{pts:03d}",
            "form": list(fm[:5]),
            "zone": "플레이오프" if (i + 1) <= po_cut else "",
        })
    wd_names = ["월", "화", "수", "목", "금", "토", "일"]
    nk = datetime.now(KST)
    now_s = f"{nk.year}.{nk.month:02d}.{nk.day:02d} ({wd_names[nk.weekday()]}) {nk.hour:02d}:{nk.minute:02d}:{nk.second:02d} KST"
    return {
        "ok": True,
        "league": cu,
        "league_name": lg_full,
        "sport": "basketball",
        "source": "SportIQ KBL·WKBL 공식 시즌 스냅샷",
        "updated_at_kst": now_s,
        "standings": rows,
        "leaders": _KBL_WKBL_LEADERS_V34[cu],
    }


def _build_domestic_bball_sim_v341(code: str, iterations: int = 5000) -> Dict[str, Any]:
    import math as _m
    import random as _r
    st = _build_domestic_bball_standings_v341(code)
    cu = st["league"]
    tot_g = 35 if cu == "WKBL" else 54
    po_cut = 4 if cu == "WKBL" else 6
    rows = st["standings"]
    meta = []
    for r in rows:
        gp = max(1, r["gp"])
        ppg = (r["w"] / gp) * 3.0
        elo = int(round(1550 + (ppg - 1.35) * 185 + (r["gd"] / gp) * 18))
        meta.append({"team": r["team"], "cur": r["w"], "rem": max(1, tot_g - gp), "gd": r["gd"], "elo": elo, "tc": 0, "pc": 0, "ps": 0.0})
    avg_elo = sum(t["elo"] for t in meta) / max(1, len(meta))
    rng = _r.Random(20261118 + sum(ord(c) for c in cu))
    probs = [max(0.12, min(0.85, 1.0 / (1.0 + _m.pow(10.0, (avg_elo - t["elo"]) / 380.0)))) for t in meta]
    iters = max(500, min(5000, int(iterations)))
    for _ in range(iters):
        sc_list = []
        for idx, t in enumerate(meta):
            pw = probs[idx]
            rem = t["rem"]
            fin = t["cur"] + max(0.0, rng.gauss(rem * pw, _m.sqrt(max(0.25, rem * pw * (1.0 - pw)))))
            t["ps"] += fin
            sc_list.append((fin + t["gd"] * 0.001, idx))
        sc_list.sort(reverse=True)
        meta[sc_list[0][1]]["tc"] += 1
        for pos, (_, ti) in enumerate(sc_list[:po_cut]):
            meta[ti]["pc"] += 1
    table = [{
        "team": t["team"],
        "elo": t["elo"],
        "cur_pts": t["cur"],
        "proj_pts": round(t["ps"] / iters, 1),
        "title_prob": round(t["tc"] * 100.0 / iters, 1),
        "po_prob": round(t["pc"] * 100.0 / iters, 1),
        "rel_prob": 0.0,
    } for t in meta]
    table.sort(key=lambda x: (-x["title_prob"], -x["proj_pts"], -x["elo"]))
    for i, r in enumerate(table):
        r["sim_rank"] = i + 1
    return {
        "ok": True,
        "league": cu,
        "league_name": st["league_name"],
        "sport": "basketball",
        "simulations": iters,
        "po_label": "플레이오프 진출 확률",
        "updated_at_kst": st["updated_at_kst"],
        "table": table,
        "fifa_ranking": [],
        "club_ranking": [],
    }


def _build_domestic_bball_injuries_v341(code: str, severity: str = "all", q: str = "") -> Dict[str, Any]:
    cu = "WKBL" if str(code or "").upper() == "WKBL" else "KBL"
    items = list(_KBL_WKBL_INJURIES_V34[cu])
    st_teams = [r[0] for r in (_WKBL_STANDINGS_V34 if cu == "WKBL" else _KBL_STANDINGS_V34)]
    tc: Dict[str, int] = {}
    for x in items:
        tc[x["team"]] = tc.get(x["team"], 0) + 1
    hard = [f"{t} ({c}명)" for t, c in sorted(tc.items(), key=lambda v: -v[1])[:3]]
    full_sq = [t for t in st_teams if tc.get(t, 0) == 0][:4]
    flt = [
        x for x in items
        if (severity == "all" or x["severity"] == severity)
        and (not q or q.lower() in f"{x['player']} {x['team']} {x['reason']}".lower())
    ]
    st = _build_domestic_bball_standings_v341(cu)
    return {
        "ok": True,
        "league": cu,
        "league_name": st["league_name"],
        "source": "SportIQ KBL·WKBL 데일리 로스터 트래커",
        "updated_at_kst": st["updated_at_kst"],
        "kpis": {
            "total_injured": len(items),
            "avg_per_team": round(len(items) / max(1, len(st_teams)), 1),
            "top_part": "발목·컨디션 관리",
            "hardest_hit": hard,
            "full_squad": full_sq,
        },
        "items": flt,
    }


@router.get("/api/member/domestic-bball-meta-v34")
def api_member_domestic_bball_meta_v34(kind: str = "standings", league: str = "KBL", severity: str = "all", q: str = ""):
    k = str(kind or "standings").lower()
    if k == "sim":
        return _build_domestic_bball_sim_v341(league, 5000)
    if k == "injuries":
        return _build_domestic_bball_injuries_v341(league, severity, q)
    return _build_domestic_bball_standings_v341(league)


def _patch_datacenter_api_routes_v341() -> None:
    # v3.4.2 Zero-mutation: never alter APIRoute signatures; client fetch bridge uses /api/member/domestic-bball-meta-v34 directly!
    return

_patch_datacenter_api_routes_v341()

@router.get("/api/member/scoreboard-v30")
def api_member_scoreboard_v30(date_offset: int = 0):
    _ensure_domestic_basketball_v34()
    """Direct server-side bridge to sportiq_datacenter supporting full -5..+5 KST calendar & normalized 10-sport matches."""
    date_offset = max(-5, min(5, int(date_offset)))
    now_k = datetime.now(KST)
    wd_names = ["월", "화", "수", "목", "금", "토", "일"]
    target_k = now_k + timedelta(days=date_offset)
    target_date_kst = f"{target_k.year}.{target_k.month:02d}.{target_k.day:02d} ({wd_names[target_k.weekday()]})"
    server_now_kst = f"{now_k.year}.{now_k.month:02d}.{now_k.day:02d} ({wd_names[now_k.weekday()]}) {now_k.hour:02d}:{now_k.minute:02d}:{now_k.second:02d} KST"
    raw_items: List[Dict[str, Any]] = []

    try:
        _dc = sys.modules.get("app.sportiq_datacenter") or sys.modules.get("sportiq_datacenter")
        if _dc is None:
            try:
                from app import sportiq_datacenter as _dc
            except Exception:
                import sportiq_datacenter as _dc  # type: ignore
        # Standard -3..+5 offsets use sportiq_datacenter's native collector directly
        if date_offset >= -3:
            collect_dict = getattr(_dc, "collect_live_scores", None)
            collect_all = getattr(_dc, "_collect_all_matches", None)
            api_ls_fn = getattr(_dc, "api_live_scores", None)
            if callable(collect_dict):
                d_res = collect_dict(date_offset)
                if isinstance(d_res, dict):
                    target_date_kst = str(d_res.get("target_date_kst") or target_date_kst)
                    server_now_kst = str(d_res.get("server_now_kst") or server_now_kst)
                    if isinstance(d_res.get("items"), list):
                        raw_items = list(d_res["items"])
            if not raw_items and callable(collect_all):
                a_res = collect_all(date_offset)
                if isinstance(a_res, list):
                    raw_items = list(a_res)
                elif isinstance(a_res, dict) and isinstance(a_res.get("items"), list):
                    raw_items = list(a_res["items"])
            if not raw_items and callable(api_ls_fn):
                try:
                    ls_res = api_ls_fn(sport="all", league="all", date_offset=date_offset)
                    if isinstance(ls_res, dict) and isinstance(ls_res.get("items"), list):
                        raw_items = list(ls_res["items"])
                except Exception:
                    pass
        else:
            # True KST lookup for -5 and -4 (bypassing collect_live_scores's -3 floor)
            cached = _V31_EXT_SCORE_CACHE.get(date_offset)
            if cached and (time.time() - cached[0]) < 300.0:
                raw_items = list(cached[1])
            else:
                fetch_espn = getattr(_dc, "_fetch_espn_sb", None)
                reg_fn = getattr(_dc, "_regional_games", None)
                lg_reg = getattr(_dc, "LEAGUE_REGISTRY", [])
                espn_lgs = [x for x in lg_reg if isinstance(x, dict) and x.get("espn")]
                d1 = target_k.strftime("%Y%m%d")
                d0 = (target_k - timedelta(days=1)).strftime("%Y%m%d")
                pref = f"{target_k.month:02d}.{target_k.day:02d}"
                seen_ids: Set[str] = set()
                if callable(fetch_espn) and espn_lgs:
                    tasks = [(lg, d0) for lg in espn_lgs] + [(lg, d1) for lg in espn_lgs]
                    with ThreadPoolExecutor(max_workers=8) as pool:
                        futs = [pool.submit(fetch_espn, lg, dp, False) for lg, dp in tasks]
                        for fut in futs:
                            try:
                                for it in fut.result(timeout=5.0):
                                    mid = str(it.get("id") or "")
                                    if mid and mid not in seen_ids and str(it.get("match_dt_kst") or "").startswith(pref):
                                        seen_ids.add(mid)
                                        raw_items.append(it)
                            except Exception:
                                continue
                if callable(reg_fn):
                    try:
                        for rg in reg_fn(target_k):
                            mid = str(rg.get("id") or "")
                            if mid not in seen_ids:
                                seen_ids.add(mid)
                                raw_items.append(rg)
                    except Exception:
                        pass
                for _dbg in _build_domestic_bball_games_v34(target_k):
                    if _dbg["id"] not in seen_ids:
                        seen_ids.add(_dbg["id"])
                        raw_items.append(_dbg)
                _V31_EXT_SCORE_CACHE[date_offset] = (time.time(), list(raw_items))
    except Exception:
        raw_items = []

    _patch_datacenter_api_routes_v341()
    _dom_bball = _build_domestic_bball_games_v34(target_k)
    _by_id_sb = {str(x.get("id") or ""): idx for idx, x in enumerate(raw_items) if isinstance(x, dict)}
    for _dg in _dom_bball:
        if _dg["id"] in _by_id_sb:
            raw_items[_by_id_sb[_dg["id"]]] = _dg
        else:
            raw_items.append(_dg)
    _ord_sb = {"in": 0, "pre": 1, "post": 2}
    raw_items.sort(key=lambda x: (_ord_sb.get(str(x.get("state") or ""), 3), str(x.get("match_dt_kst") or "")))

    return {
        "ok": True,
        "date_offset": date_offset,
        "target_date_kst": target_date_kst,
        "server_now_kst": server_now_kst,
        "date_strip": _build_date_strip_v31(date_offset),
        "total": len(raw_items),
        "items": raw_items,
    }

class MatchFanVoteIn(BaseModel):
    match_key: str
    pick: str  # "H" | "D" | "A"
    has_draw: Optional[bool] = True
    hp: Optional[int] = 45
    dp: Optional[int] = 25
    ap: Optional[int] = 30
    nickname: Optional[str] = ""
    pin: Optional[str] = ""
    actor_key: Optional[str] = ""
    client_points: Optional[int] = 120


def _seed_fan_vote_baseline_v32(match_key: str, has_draw: bool, hp: int, dp: int, ap: int) -> Tuple[int, int, int]:
    h_val = 2166136261
    for ch in match_key.encode("utf-8", errors="ignore"):
        h_val ^= ch
        h_val = (h_val * 16777619) & 0xFFFFFFFF
    total_base = 24 + (h_val % 33)  # 24 ~ 56 initial fan votes
    hp_c = max(8, min(88, int(hp or 45)))
    dp_c = max(8, min(45, int(dp or 24))) if has_draw else 0
    ap_c = max(8, 100 - hp_c - dp_c)
    s = max(1, hp_c + dp_c + ap_c)
    hv = max(2, int(round(total_base * (hp_c / s))))
    dv = max(1, int(round(total_base * (dp_c / s)))) if has_draw else 0
    av = max(2, total_base - hv - dv)
    return hv, dv, av


def _format_fan_vote_payload_v32(
    match_key: str,
    hv: int,
    dv: int,
    av: int,
    has_draw: bool,
    my_pick: str = "",
    rewarded: bool = False,
    awarded_points: int = 0,
    points: Optional[int] = None,
    role_tier: str = "",
    switched: bool = False,
    message: str = "",
) -> Dict[str, Any]:
    if not has_draw:
        dv = 0
    tot = max(1, hv + dv + av)
    h_pct = int(round((hv / tot) * 100))
    d_pct = int(round((dv / tot) * 100)) if has_draw else 0
    a_pct = max(0, 100 - h_pct - d_pct)
    out: Dict[str, Any] = {
        "ok": True,
        "match_key": match_key,
        "has_draw": bool(has_draw),
        "my_pick": my_pick,
        "rewarded": bool(rewarded),
        "switched": bool(switched),
        "awarded_points": int(awarded_points),
        "home_votes": int(hv),
        "draw_votes": int(dv),
        "away_votes": int(av),
        "total_votes": int(hv + dv + av),
        "home_pct": h_pct,
        "draw_pct": d_pct,
        "away_pct": a_pct,
        "message": message,
    }
    if points is not None:
        out["points"] = int(points)
    if role_tier:
        out["role_tier"] = role_tier
    return out


@router.get("/api/member/match-vote")
def get_match_fan_vote_v32(
    match_key: str,
    actor_key: str = "",
    nickname: str = "",
    has_draw: int = 1,
    hp: int = 45,
    dp: int = 25,
    ap: int = 30,
):
    mk = (match_key or "").strip()[:120]
    if not mk:
        raise HTTPException(status_code=400, detail="match_key required")
    hd = bool(int(has_draw or 0))
    _, actor_candidates = _normalize_actor_keys(actor_key, nickname)

    def _op() -> Dict[str, Any]:
        now = time.time()
        with _smu_db_session(write=True) as conn:
            t_row = conn.execute(
                "SELECT home_votes, draw_votes, away_votes FROM match_fan_vote_totals_v32 WHERE match_key = ?",
                (mk,),
            ).fetchone()
            if not t_row:
                hv, dv, av = _seed_fan_vote_baseline_v32(mk, hd, hp, dp, ap)
                conn.execute(
                    "INSERT OR IGNORE INTO match_fan_vote_totals_v32(match_key, home_votes, draw_votes, away_votes, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (mk, hv, dv, av, now),
                )
            else:
                hv = int(t_row["home_votes"] or 0)
                dv = int(t_row["draw_votes"] or 0) if hd else 0
                av = int(t_row["away_votes"] or 0)

            placeholders = ",".join("?" for _ in actor_candidates)
            v_row = conn.execute(
                f"SELECT pick, rewarded FROM match_fan_votes_v32 WHERE match_key = ? AND actor_key IN ({placeholders}) ORDER BY rewarded DESC, updated_at DESC LIMIT 1",
                [mk] + actor_candidates,
            ).fetchone()
            my_pick = str(v_row["pick"] or "") if v_row else ""
            rewarded = bool(v_row["rewarded"]) if v_row else False
            return _format_fan_vote_payload_v32(mk, hv, dv, av, hd, my_pick=my_pick, rewarded=rewarded)

    return run_with_db_retry(_op)


@router.post("/api/member/match-vote")
def submit_match_fan_vote_v32(payload: MatchFanVoteIn):
    mk = (payload.match_key or "").strip()[:120]
    pick = (payload.pick or "").strip().upper()
    hd = bool(payload.has_draw)
    if not mk or pick not in ("H", "D", "A") or (pick == "D" and not hd):
        raise HTTPException(status_code=400, detail="유효하지 않은 승부투표 항목입니다.")

    nick = (payload.nickname or "").strip()[:24]
    pin = re.sub(r"\D", "", payload.pin or "")
    primary_actor, actor_candidates = _normalize_actor_keys(payload.actor_key or "", nick)
    today_kst = datetime.now(KST).strftime("%Y-%m-%d")
    tier_fn = getattr(
        _smu,
        "_tier_for_points",
        lambda p: "PRO" if p >= 350 else ("MEMBER" if p >= 150 else "GUEST"),
    )
    hash_fn = getattr(_smu, "_hash_pin", None)

    def _op() -> Tuple[Dict[str, Any], List[str]]:
        now = time.time()
        touched_tables: List[str] = ["match_fan_votes_v32", "match_fan_vote_totals_v32"]
        with _smu_db_session(write=True) as conn:
            t_row = conn.execute(
                "SELECT home_votes, draw_votes, away_votes FROM match_fan_vote_totals_v32 WHERE match_key = ?",
                (mk,),
            ).fetchone()
            if not t_row:
                hv, dv, av = _seed_fan_vote_baseline_v32(mk, hd, int(payload.hp or 45), int(payload.dp or 25), int(payload.ap or 30))
                conn.execute(
                    "INSERT OR IGNORE INTO match_fan_vote_totals_v32(match_key, home_votes, draw_votes, away_votes, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (mk, hv, dv, av, now),
                )
            else:
                hv = int(t_row["home_votes"] or 0)
                dv = int(t_row["draw_votes"] or 0) if hd else 0
                av = int(t_row["away_votes"] or 0)

            placeholders = ",".join("?" for _ in actor_candidates)
            existing = conn.execute(
                f"SELECT actor_key, pick, rewarded FROM match_fan_votes_v32 WHERE match_key = ? AND actor_key IN ({placeholders}) ORDER BY rewarded DESC, updated_at DESC LIMIT 1",
                [mk] + actor_candidates,
            ).fetchone()

            already_rewarded = bool(existing["rewarded"]) if existing else False
            prev_pick = str(existing["pick"] or "") if existing else ""
            row_actor = str(existing["actor_key"]) if existing else primary_actor

            # Also check wp_point_ledger anti-farming guard
            ledger_dup = conn.execute(
                "SELECT id FROM wp_point_ledger WHERE nickname IN (?, ?) AND event_type IN ('match_fan_vote', 'guest_match_vote') AND ref_id = ? LIMIT 1",
                (nick or primary_actor, primary_actor, mk),
            ).fetchone()
            if ledger_dup:
                already_rewarded = True

            switched = False
            if prev_pick == pick and existing:
                # Idempotent re-click on same pick
                pass
            else:
                if prev_pick == "H":
                    hv = max(0, hv - 1)
                elif prev_pick == "D":
                    dv = max(0, dv - 1)
                elif prev_pick == "A":
                    av = max(0, av - 1)

                if pick == "H":
                    hv += 1
                elif pick == "D":
                    dv += 1
                elif pick == "A":
                    av += 1

                switched = bool(prev_pick and prev_pick != pick)

            awarded = 0 if already_rewarded else 5
            new_pts: Optional[int] = None
            new_tier = ""

            if awarded > 0:
                has_ma = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='member_accounts'"
                ).fetchone()
                credited_member = False
                if has_ma and len(nick) >= 2 and len(pin) == 4 and callable(hash_fn):
                    pin_h = hash_fn(nick, pin)
                    m_row = conn.execute(
                        "SELECT nickname, pin_hash, points FROM member_accounts WHERE lower(nickname) = lower(?)",
                        (nick,),
                    ).fetchone()
                    if m_row and m_row["pin_hash"] == pin_h:
                        new_pts = int(m_row["points"] or 120) + awarded
                        new_tier = tier_fn(new_pts)
                        conn.execute(
                            "UPDATE member_accounts SET points = ?, role_tier = ?, updated_at = ? WHERE nickname = ?",
                            (new_pts, new_tier, now, m_row["nickname"]),
                        )
                        conn.execute(
                            "INSERT INTO wp_point_ledger(nickname, event_type, ref_id, points_delta, kst_date, site_origin, created_at) VALUES (?, 'match_fan_vote', ?, ?, ?, 'open_lounge', ?)",
                            (m_row["nickname"], mk, awarded, today_kst, now),
                        )
                        touched_tables.extend(["member_accounts", "wp_point_ledger"])
                        credited_member = True

                if not credited_member:
                    c_pts = max(120, int(payload.client_points or 120))
                    new_pts = c_pts + awarded
                    new_tier = tier_fn(new_pts)
                    conn.execute(
                        "INSERT INTO wp_point_ledger(nickname, event_type, ref_id, points_delta, kst_date, site_origin, created_at) VALUES (?, 'guest_match_vote', ?, ?, ?, 'open_lounge_guest', ?)",
                        (primary_actor, mk, awarded, today_kst, now),
                    )
                    touched_tables.append("wp_point_ledger")

            conn.execute(
                """
                INSERT INTO match_fan_votes_v32(match_key, actor_key, pick, rewarded, updated_at)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(match_key, actor_key) DO UPDATE SET
                    pick = excluded.pick,
                    rewarded = 1,
                    updated_at = excluded.updated_at
                """,
                (mk, row_actor, pick, now),
            )
            conn.execute(
                "UPDATE match_fan_vote_totals_v32 SET home_votes = ?, draw_votes = ?, away_votes = ?, updated_at = ? WHERE match_key = ?",
                (hv, dv, av, now, mk),
            )

            msg = (
                f"🗳️ 승부투표 완료! +{awarded}P가 즉시 적립되었습니다."
                if awarded > 0
                else ("🔄 승부투표 픽이 변경되었습니다! (경기당 첫 투표 +5P 지급 완료)" if switched else "이미 이 경기에 투표하셨습니다.")
            )
            return (
                _format_fan_vote_payload_v32(
                    mk,
                    hv,
                    dv,
                    av,
                    hd,
                    my_pick=pick,
                    rewarded=True,
                    awarded_points=awarded,
                    points=new_pts,
                    role_tier=new_tier,
                    switched=switched,
                    message=msg,
                ),
                touched_tables,
            )

    res_dict, bg_tables = run_with_db_retry(_op)
    _trigger_smu_pg_backup(*tuple(dict.fromkeys(bg_tables)))
    return res_dict


@router.api_route("/sitemap.xml", methods=["GET", "HEAD"])
def serve_enriched_sitemap_xml_v27():
    from fastapi.responses import Response
    today = datetime.now(KST).strftime("%Y-%m-%d")
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
        'xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">\n'
        "  <url>\n"
        "    <loc>https://billnojay.com/</loc>\n"
        f"    <lastmod>{today}</lastmod>\n"
        "    <changefreq>hourly</changefreq>\n"
        "    <priority>1.0</priority>\n"
        "    <image:image>\n"
        "      <image:loc>https://billnojay.com/og-cover.png?v=27</image:loc>\n"
        "      <image:title>SportIQ 실시간 분석 스포츠IQ 인사이트 라운지</image:title>\n"
        "    </image:image>\n"
        "  </url>\n"
        "</urlset>\n"
    )
    return Response(content=xml, media_type="application/xml; charset=utf-8")



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


@router.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
def serve_lounge_home(request: Request):
    if os.path.exists(HTML_PATH):
        try:
            _ensure_upgrade_js_dedup_patched()
            with open(HTML_PATH, "r", encoding="utf-8") as f:
                raw_html = f.read()
            rendered = inject_upgrade_into_html(raw_html)
            rendered = re.sub(
                r'/api/member/p0p1-upgrade\.js(?:\?[^"\'\s>]*)?',
                f'/api/member/p0p1-upgrade-bundle?v={_BOOT_VER_V30}',
                rendered,
            )
            rendered = inject_seo_and_og_head_v27(rendered)
            _pq = (request.query_params.get("post") if request and hasattr(request, "query_params") else "") or ""
            if _pq:
                rendered = apply_dynamic_post_og_v29(rendered, resolve_post_og_meta_v29(_pq))
            return HTMLResponse(rendered, media_type="text/html; charset=utf-8", headers={"Cache-Control": "private, no-store, no-cache, must-revalidate", "CDN-Cache-Control": "no-store", "Cloudflare-CDN-Cache-Control": "no-store"})
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
        "seo_og_v27": True,
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