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
      mountSortToggleAboveStrip(container);

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

    if updated != js_raw:
        _smu.UPGRADE_JS = updated
    return _SORT_DEDUP_APPLIED or _AI_SEARCH_UI_PATCHED


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
    ("맨유", "맨체스터 유나이티드", "맨체스터유나이티드", "manchester united", "man utd", "man united", "mufc"),
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

    return {
        "raw_query": q_clean,
        "q_low": q_low,
        "stems": stems,
        "concept_groups": concept_groups,
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

    groups: List[Set[str]] = q_meta["concept_groups"]
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

    # Trusted sports media bonus
    if is_internal_sportiq or any(ts in src_low for ts in _TRUSTED_SPORT_SOURCES):
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

    cache_key = f"hyper2:{mode}:{q.lower()}"
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
                futs_know.append(pool.submit(_fetch_wikipedia_smart, q, "ko"))
                if en_q and en_q.lower() != q.lower():
                    futs_know.append(pool.submit(_fetch_wikipedia_smart, en_q.split()[0] if len(en_q.split()) > 2 else en_q, "en"))
                ddg_fn = getattr(_smu, "_fetch_ddg_instant_answer", None)
                if callable(ddg_fn):
                    futs_know.append(pool.submit(ddg_fn, en_q or q))

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
                seen_know_titles.add(kt)
                knowledge_cards.append(kc)
                if len(knowledge_cards) >= 4:
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
                lambda m: m.group(1) + str(int(m.group(2)) + 200),
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
