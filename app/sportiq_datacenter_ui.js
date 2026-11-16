(function(){
  if(window.__dcWidgetLoaded)return;
  window.__dcWidgetLoaded=true;

  function dcEsc(s){
    if(s===null||s===undefined)return '';
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
  }
  function dcNormTeamNames(obj){if(!obj)return obj;var lg=String(obj.league||obj.league_code||'').toUpperCase(),sp=String(obj.sport||'').toLowerCase(),h=String(obj.home||'').trim(),a=String(obj.away||'').trim();if(lg==='F1'||sp==='f1'){if(!h||h==='Home')obj.home='베르스타펜 (레드불)';if(!a||a==='Away')obj.away='노리스 (맥라렌)';if(obj.status_badge&&String(obj.status_badge).indexOf('EST')!==-1)obj.status_badge='퀄리파잉 · 결승 세션';if(obj.status_text&&String(obj.status_text).indexOf('EST')!==-1)obj.status_text='퀄리파잉 · 결승 세션';}else if(lg==='WTA'){if(!h||h==='Home')obj.home='사발렌카';if(!a||a==='Away')obj.away='시비옹테크';}else if(lg==='ATP'||sp==='tennis'){if(!h||h==='Home')obj.home='신네르';if(!a||a==='Away')obj.away='알카라스';}else{if((!h||h==='Home')&&(!a||a==='Away')){obj.home='홈';obj.away='원정';}}if(obj.match&&String(obj.match).indexOf('Home vs Away')!==-1){var rep=(lg==='F1'||sp==='f1')?'베르스타펜 (레드불) vs 노리스 (맥라렌)':(lg==='WTA'?'사발렌카 vs 시비옹테크':((lg==='ATP'||sp==='tennis')?'신네르 vs 알카라스':'홈 vs 원정'));obj.match=String(obj.match).replace(/Home vs Away/g,rep);}return obj;} function dcNum(n,d){
    var v=Number(n);
    return isNaN(v)?'-':(d!==undefined?v.toFixed(d):String(v));
  }
  function dcGetJson(url,cb){
    fetch(url,{credentials:'same-origin'}).then(function(r){return r.json();}).then(function(d){cb(null,d);}).catch(function(e){cb(e,null);});
  }
  function dcPostJson(url,cb){
    fetch(url,{method:'POST',credentials:'same-origin'}).then(function(r){return r.json();}).then(function(d){cb(null,d);}).catch(function(e){cb(e,null);});
  }

  var css = [
    '#dcTickerControls{display:flex;flex-wrap:wrap;align-items:center;gap:6px;width:100%;padding:6px 0;border-top:1px solid rgba(51,65,85,.7);margin-top:4px}',
    '.dc-pill{background:#0f172a;color:#cbd5e1;border:1px solid #334155;border-radius:999px;padding:4px 10px;font-size:12px;cursor:pointer;display:inline-flex;align-items:center;gap:4px;white-space:nowrap;transition:all .15s}',
    '.dc-pill:hover{border-color:#38bdf8;color:#fff}',
    '.dc-pill.active{background:#0284c7;border-color:#38bdf8;color:#fff;font-weight:700}',
    '.dc-select{background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:8px;padding:4px 8px;font-size:12px}',
    '#dcLiveTickerTrack{display:flex;gap:10px;overflow-x:auto;padding:8px 2px;width:100%;scrollbar-width:thin}',
    '.dc-mcard{min-width:255px;max-width:270px;flex:0 0 auto;background:#0f172a;border:1px solid #1e293b;border-radius:12px;padding:10px 12px;cursor:pointer;transition:border-color .15s,transform .15s}',
    '.dc-mcard:hover{border-color:#38bdf8;transform:translateY(-1px)}',
    '.dc-mcard-top{display:flex;justify-content:space-between;align-items:center;gap:4px;font-size:11px;color:#94a3b8;margin-bottom:6px}',
    '.dc-lbadge{background:#1e293b;color:#38bdf8;padding:2px 6px;border-radius:6px;font-weight:700;font-size:10px}',
    '.dc-sbadge{padding:2px 6px;border-radius:6px;font-weight:700;font-size:10px}',
    '.dc-sbadge.in{background:rgba(239,68,68,.2);color:#f87171;border:1px solid rgba(239,68,68,.4)}',
    '.dc-sbadge.pre{background:rgba(56,189,248,.15);color:#38bdf8}',
    '.dc-sbadge.post{background:rgba(148,163,184,.15);color:#94a3b8}',
    '.dc-trow{display:flex;justify-content:space-between;align-items:center;font-size:13px;font-weight:600;color:#f1f5f9;margin:2px 0}',
    '.dc-score{font-size:15px;font-weight:800;color:#38bdf8}',
    '.dc-periods{font-size:11px;color:#94a3b8;margin-top:5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
    '.dc-airow{font-size:11px;color:#a5f3fc;background:rgba(14,116,144,.2);border-radius:6px;padding:4px 6px;margin-top:6px}',
    '#dcMainHubSection{max-width:80rem;margin:14px auto;padding:0 16px}',
    '.dc-hub-wrap{background:#0b1220;border:1px solid #1e293b;border-radius:16px;padding:16px;color:#e2e8f0;box-shadow:0 10px 30px rgba(0,0,0,.35)}',
    '.dc-hub-head{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:10px;padding-bottom:12px;border-bottom:1px solid #1e293b}',
    '.dc-live-dot{display:inline-flex;align-items:center;gap:6px;background:rgba(16,185,129,.15);color:#34d399;border:1px solid rgba(16,185,129,.4);padding:3px 9px;border-radius:999px;font-size:11px;font-weight:700}',
    '.dc-btn{background:#1e293b;color:#e2e8f0;border:1px solid #334155;border-radius:8px;padding:6px 12px;font-size:12px;font-weight:600;cursor:pointer}',
    '.dc-btn:hover{border-color:#38bdf8;color:#fff}',
    '.dc-btn-primary{background:#0284c7;border-color:#38bdf8;color:#fff}',
    '.dc-launcher-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin:14px 0}',
    '.dc-lcard{background:#111a2e;border:1px solid #1e293b;border-radius:12px;padding:12px;cursor:pointer;text-align:left;transition:all .15s}',
    '.dc-lcard:hover{border-color:#38bdf8}',
    '.dc-lcard.active{background:linear-gradient(135deg,#0c2444,#0f172a);border-color:#38bdf8;box-shadow:0 0 0 1px #38bdf8}',
    '.dc-lcard-title{font-size:13px;font-weight:700;color:#f8fafc}',
    '.dc-lcard-sub{font-size:11px;color:#94a3b8;margin-top:3px}',
    '.dc-grid-2{display:grid;grid-template-columns:1.35fr 1fr;gap:14px;align-items:start}',
    '.dc-kpi-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin-bottom:14px}',
    '.dc-kpi{background:#111a2e;border:1px solid #1e293b;border-radius:10px;padding:10px 12px}',
    '.dc-kpi-lbl{font-size:11px;color:#94a3b8}',
    '.dc-kpi-val{font-size:15px;font-weight:700;color:#f8fafc;margin-top:3px}',
    '.dc-card{background:#111a2e;border:1px solid #1e293b;border-radius:12px;padding:12px;overflow-x:auto}',
    '.dc-tbl{width:100%;border-collapse:collapse;font-size:12px}',
    '.dc-tbl th{text-align:left;color:#94a3b8;font-weight:600;padding:7px 6px;border-bottom:1px solid #1e293b;white-space:nowrap}',
    '.dc-tbl td{padding:7px 6px;border-bottom:1px solid rgba(30,41,59,.6);color:#e2e8f0;white-space:nowrap}',
    '.dc-zone-ucl{border-left:3px solid #10b981;padding-left:6px}',
    '.dc-zone-uel{border-left:3px solid #38bdf8;padding-left:6px}',
    '.dc-zone-po{border-left:3px solid #f59e0b;padding-left:6px}',
    '.dc-zone-rel{border-left:3px solid #ef4444;padding-left:6px}',
    '.dc-dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:2px}',
    '.dc-dot-W{background:#22c55e}.dc-dot-D{background:#94a3b8}.dc-dot-L{background:#ef4444}',
    '.dc-bar-bg{width:64px;height:7px;background:#1e293b;border-radius:999px;display:inline-block;overflow:hidden;vertical-align:middle;margin-right:4px}',
    '.dc-bar-fg{height:100%;background:#38bdf8;display:block}',
    '.dc-hl-row{background:rgba(16,185,129,.12)}',
    '#dcToastWrap{position:fixed;top:16px;right:16px;z-index:99999;display:flex;flex-direction:column;gap:8px;max-width:340px}',
    '.dc-toast{background:#0f172a;color:#f8fafc;border:1px solid #38bdf8;border-radius:10px;padding:10px 14px;font-size:12px;box-shadow:0 8px 24px rgba(0,0,0,.45)}',
    '#dcMatchModal{position:fixed;inset:0;background:rgba(2,6,23,.78);z-index:99998;display:none;align-items:center;justify-content:center;padding:16px}',
    '.dc-modal-box{background:#0f172a;border:1px solid #334155;border-radius:16px;max-width:500px;width:100%;padding:18px;color:#f1f5f9}',
    '@media(max-width:860px){.dc-grid-2{grid-template-columns:1fr}.dc-launcher-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}'
  ].join('\n');
  var st=document.createElement('style');
  st.textContent=css;
  document.head.appendChild(st);

  var toastWrap=document.createElement('div');
  toastWrap.id='dcToastWrap';
  document.body.appendChild(toastWrap);

  function showToast(msg){
    var el=document.createElement('div');
    el.className='dc-toast';
    el.textContent=msg;
    toastWrap.appendChild(el);
    setTimeout(function(){if(el.parentNode)el.parentNode.removeChild(el);},4500);}function showOddsToast(msg){var el=document.createElement('div');el.className='dc-toast';el.style.cssText='background:linear-gradient(135deg,rgba(124,58,237,0.96),rgba(217,119,6,0.95));color:#fff;border:1px solid rgba(251,191,36,0.6);box-shadow:0 8px 24px rgba(88,28,135,0.45);font-weight:700;';el.textContent=msg;toastWrap.appendChild(el);setTimeout(function(){if(el.parentNode)el.parentNode.removeChild(el);},5200);
  }
  function playBeep(){
    try{
      var Ctx=window.AudioContext||window.webkitAudioContext;
      if(!Ctx)return;
      var ctx=new Ctx();
      var osc=ctx.createOscillator();
      var gain=ctx.createGain();
      osc.type='sine';
      osc.frequency.value=880;
      gain.gain.value=0.06;
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start();
      setTimeout(function(){osc.stop();ctx.close();},180);
    }catch(e){}
  }

  var state={
    sport:'all',
    league:'all',oddsAlertOn:localStorage.getItem('dc_odds_alert')!=='0',seenOddsAlerts:{},counts:{},dropSport:'all',dropMin:5,
    dateOffset:0,
    alertOn:localStorage.getItem('sportiq_dc_score_alert')!=='0',
    prevScores:{},
    seenAlerts:{},
    matches:[],
    tab:'standings',
    collapsed:false,
    standingsLeague:'EPL',
    seasonLeague:'EPL',
    transferCat:'ALL',
    transferFilter:'TOP',
    injuryLeague:'EPL',
    injurySeverity:'ALL',
    injuryQuery:''
  };

  var SPORTS=[
    {id:'all',lbl:'🌐 전체',cntKey:'all'},
    {id:'soccer',lbl:'⚽ 축구',cntKey:'soccer'},
    {id:'baseball',lbl:'⚾ 야구',cntKey:'baseball'},
    {id:'basketball',lbl:'🏀 농구',cntKey:'basketball'},{id:'volleyball',lbl:'🏐 배구(V-리그)',cntKey:'volleyball'},
    {id:'hockey',lbl:'🏒 하키',cntKey:'hockey'},
    {id:'esports',lbl:'🎮 LCK',cntKey:'esports'},{id:'ufc',lbl:'🥊 UFC',cntKey:'ufc'},{id:'tennis',lbl:'🎾 테니스',cntKey:'tennis'},{id:'f1',lbl:'🏎️ F1',cntKey:'f1'}
  ];
  var LEAGUES18=['all','EPL','LALIGA','BUNDESLIGA','SERIEA','LIGUE1','KLEAGUE','JLEAGUE','MLS','UCL','UNL','WC2026','KBO','MLB','NPB','NBA','WNBA','VLEAGUE_M','VLEAGUE_W','NHL','LCK','UFC','ATP','WTA','F1'];
  var LEAGUES11=['EPL','LALIGA','BUNDESLIGA','SERIEA','LIGUE1','KLEAGUE','NBA','KBO','MLB','NHL','LCK'];

  var modal=document.createElement('div');
  modal.id='dcMatchModal';
  modal.innerHTML='<div class="dc-modal-box" id="dcMatchModalBody"></div>';
  modal.onclick=function(e){if(e.target===modal)modal.style.display='none';};
  document.body.appendChild(modal);

  function openMatchModal(m){
    var box=document.getElementById('dcMatchModalBody');
    var pstr=(m.period_scores&&m.period_scores.length)?m.period_scores.join(' / '):'세부 스코어 집계 중';
    var ai=m.ai||{};
    var odds=ai.odds||{};
    var lgCode=m.league_code||m.league||'EPL';
    var hasLeague=LEAGUES11.indexOf(lgCode)>=0?lgCode:'EPL';
    box.innerHTML=
      '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">'
      +'<span class="dc-lbadge">'+dcEsc(m.league||lgCode)+'</span>'
      +'<button class="dc-btn" id="dcCloseModalBtn">닫기 ✕</button>'
      +'</div>'
      +'<div style="font-size:12px;color:#94a3b8;margin-bottom:8px">📅 '+dcEsc(m.match_dt_full_kst||m.match_dt_kst)+' · 🏟️ '+dcEsc(m.venue||'구장 정보 없음')+'</div>'
      +'<div style="display:flex;justify-content:space-between;align-items:center;background:#1e293b;border-radius:12px;padding:14px;margin-bottom:10px">'
      +'<div style="font-size:15px;font-weight:700">'+dcEsc(m.home)+'</div>'
      +'<div style="font-size:22px;font-weight:800;color:#38bdf8">'+dcEsc(m.home_score)+' : '+dcEsc(m.away_score)+'</div>'
      +'<div style="font-size:15px;font-weight:700">'+dcEsc(m.away)+'</div>'
      +'</div>'
      +'<div style="font-size:12px;color:#cbd5e1;margin-bottom:8px">📊 세부 점수: '+dcEsc(pstr)+'</div>'
      +'<div class="dc-airow" style="margin-bottom:12px">🎯 AI 승무패 확률: 홈 '+dcEsc(ai.home_pct)+'% / 무 '+dcEsc(ai.draw_pct)+'% / 원정 '+dcEsc(ai.away_pct)+'% · 배당 ('+dcEsc(odds.home)+' / '+dcEsc(odds.draw)+' / '+dcEsc(odds.away)+')</div>'
      +'<div style="display:flex;gap:8px;flex-wrap:wrap">'
      +'<button class="dc-btn dc-btn-primary" id="dcGoStandingsBtn">🔢 '+dcEsc(hasLeague)+' 순위표 보기</button>'
      +'<button class="dc-btn" id="dcGoInjuriesBtn">🏥 '+dcEsc(hasLeague)+' 부상자 보기</button>'
      +'</div>';
    modal.style.display='flex';
    document.getElementById('dcCloseModalBtn').onclick=function(){modal.style.display='none';};
    document.getElementById('dcGoStandingsBtn').onclick=function(){
      modal.style.display='none';
      state.standingsLeague=hasLeague;
      switchHubTab('standings',true);
    };
    document.getElementById('dcGoInjuriesBtn').onclick=function(){
      modal.style.display='none';
      state.injuryLeague=hasLeague;
      switchHubTab('injuries',true);
    };
  }

  function setupTickerBar(){
    var bar=document.getElementById('proLiveTickerBar');
    if(!bar)return;
    var oldList=document.getElementById('proTickerList');
    if(oldList)oldList.style.display='none';
    var inner=bar.querySelector('.max-w-7xl')||bar;

    var ctrl=document.createElement('div');
    ctrl.id='dcTickerControls';
    inner.appendChild(ctrl);

    var track=document.createElement('div');
    track.id='dcLiveTickerTrack';
    track.innerHTML='<span style="font-size:12px;color:#94a3b8">실시간 스코어를 불러오는 중...</span>';
    inner.appendChild(track);

    window.loadProLiveTicker=loadLiveScores;
    renderTickerControls({});
    loadLiveScores();
    setInterval(loadLiveScores,25000);
  }

  function renderTickerControls(counts){
    var ctrl=document.getElementById('dcTickerControls');
    if(!ctrl)return;
    var html='';
    for(var i=0;i<SPORTS.length;i++){
      var sp=SPORTS[i];
      var cnt=counts&&counts[sp.cntKey]!==undefined?counts[sp.cntKey]:'';
      var act=state.sport===sp.id?' active':'';
      html+='<button class="dc-pill'+act+'" data-dcsport="'+dcEsc(sp.id)+'">'+dcEsc(sp.lbl)+(cnt!==''?' <span>('+dcEsc(cnt)+')</span>':'')+'</button>';
    }
    html+='<select class="dc-select" id="dcLeagueSel">';
    for(var j=0;j<LEAGUES18.length;j++){
      var lg=LEAGUES18[j];
      var lbl=lg==='all'?'전체 18개 리그':lg;
      html+='<option value="'+dcEsc(lg)+'"'+(state.league===lg?' selected':'')+'>'+dcEsc(lbl)+'</option>';
    }
    html+='</select>';
    var dates=[{v:-1,l:'◀ 어제'},{v:0,l:'오늘 · 실시간'},{v:1,l:'내일 ▶'},{v:2,l:'+2일'}];
    for(var k=0;k<dates.length;k++){
      var dt=dates[k];
      html+='<button class="dc-pill'+(state.dateOffset===dt.v?' active':'')+'" data-dcdate="'+dt.v+'">'+dcEsc(dt.l)+'</button>';
    }
    html+='<button class="dc-pill'+(state.alertOn?' active':'')+'" id="dcAlertToggleBtn">'+(state.alertOn?'🔔 득점알림 ON':'🔕 득점알림 OFF')+'</button>';
    html+='<button class="dc-pill'+(state.oddsAlertOn?' active':'')+'" id="dcOddsAlertToggleBtn">'+(state.oddsAlertOn?'📉 배당급변 알림 ON':'🔕 배당알림 OFF')+'</button>';ctrl.innerHTML=html;

    var sBtns=ctrl.querySelectorAll('[data-dcsport]');
    for(var a=0;a<sBtns.length;a++){
      sBtns[a].onclick=function(){
        state.sport=this.getAttribute('data-dcsport');
        loadLiveScores();
      };
    }
    document.getElementById('dcLeagueSel').onchange=function(){
      state.league=this.value;
      loadLiveScores();
    };
    var dBtns=ctrl.querySelectorAll('[data-dcdate]');
    for(var b=0;b<dBtns.length;b++){
      dBtns[b].onclick=function(){
        state.dateOffset=Number(this.getAttribute('data-dcdate'));
        loadLiveScores();
       var ob=document.getElementById('dcOddsAlertToggleBtn');if(ob){ob.onclick=function(){state.oddsAlertOn=!state.oddsAlertOn;localStorage.setItem('dc_odds_alert',state.oddsAlertOn?'1':'0');renderTickerControls(counts);if(state.oddsAlertOn){showOddsToast('📉 드롭 오즈 실시간 경보가 켜졌습니다.');playBeep();}else{showToast('🔕 배당급변 알림이 꺼졌습니다.');}};};
      };
    }
    document.getElementById('dcAlertToggleBtn').onclick=function(){
      state.alertOn=!state.alertOn;
      localStorage.setItem('sportiq_dc_score_alert',state.alertOn?'1':'0');
      renderTickerControls(counts);
      showToast(state.alertOn?'🔔 실시간 득점 알림이 켜졌습니다.':'🔕 실시간 득점 알림이 꺼졌습니다.');
    };
  }

  function loadLiveScores(){
    var url='/api/datacenter/live-scores?sport='+encodeURIComponent(state.sport)+'&league='+encodeURIComponent(state.league)+'&date_offset='+encodeURIComponent(state.dateOffset);
    dcGetJson(url,function(err,data){
      if(err||!data||!data.ok)return;
      renderTickerControls(data.counts||{});
      var matches=(data.matches||[]).map(dcNormTeamNames);
      state.matches=matches;
      var updated=document.getElementById('proTickerUpdatedAt');
      if(updated&&data.server_now_kst)updated.textContent='KST '+data.server_now_kst;

      var alerted=false;
      for(var i=0;i<matches.length;i++){
        var m=matches[i];
        var key=String(m.id||i);
        var cur=String(m.home_score)+'-'+String(m.away_score);
        if(state.prevScores[key]&&state.prevScores[key]!==cur){
          if(state.alertOn){
            showToast('⚽ 득점 알림 ['+m.league+'] '+m.home+' '+m.home_score+' : '+m.away_score+' '+m.away);
            alerted=true;
          }
        }
        state.prevScores[key]=cur;
      }
      var sal=data.score_alerts||[];
      for(var j=0;j<sal.length;j++){
        var al=sal[j];
        if(al.id&&!state.seenAlerts[al.id]){
          state.seenAlerts[al.id]=true;
          if(state.alertOn&&Object.keys(state.seenAlerts).length>1){
            showToast('🔔 '+al.text);
            alerted=true;
          }
        }
      }
      var oal=data.odds_alerts||[];for(var oj=0;oj<oal.length;oj++){var oa=oal[oj];if(oa.id&&!state.seenOddsAlerts[oa.id]){state.seenOddsAlerts[oa.id]=true;if(state.oddsAlertOn&&Object.keys(state.seenOddsAlerts).length>1){showOddsToast('📉 ['+(oa.league||oa.sport||'')+'] '+oa.text);alerted=true;}}}if(alerted)playBeep();

      var track=document.getElementById('dcLiveTickerTrack');
      if(!track)return;
      if(!matches.length){
        track.innerHTML='<div style="font-size:12px;color:#94a3b8;padding:10px">선택한 조건에 해당하는 경기가 없습니다.</div>';
        return;
      }
      var html='';
      for(var k=0;k<matches.length;k++){
        var x=matches[k];
        var stCls=x.state==='in'?'in':(x.state==='post'?'post':'pre');
        var stTxt=x.status_badge||(x.state==='in'?'LIVE':(x.state==='post'?'종료':'예정'));
        var pstr=(x.period_scores&&x.period_scores.length)?x.period_scores.join(' · '):'세부 스코어 대기';
        var ai=x.ai||{};
        var odds=ai.odds||{};var oddsTxt=Number(odds.draw)>0?(dcEsc(odds.home)+'/'+dcEsc(odds.draw)+'/'+dcEsc(odds.away)):(dcEsc(odds.home)+' / '+dcEsc(odds.away));var dropB=x.drop_badge?'<span style="display:inline-block;margin-left:4px;padding:1px 6px;border-radius:999px;font-size:10px;font-weight:800;background:linear-gradient(135deg,rgba(245,158,11,0.22),rgba(239,68,68,0.25));color:#fbbf24;border:1px solid rgba(245,158,11,0.55);">'+dcEsc(x.drop_badge)+'</span>':'';
        html+='<div class="dc-mcard" data-midx="'+k+'">'
          +'<div class="dc-mcard-top"><span class="dc-lbadge">'+dcEsc(x.league_code||x.league)+'</span>'+dropB+'<span>📅 '+dcEsc(x.match_dt_kst)+'</span><span class="dc-sbadge '+stCls+'">'+dcEsc(stTxt)+'</span></div>'
          +'<div class="dc-trow"><span>'+dcEsc(x.home)+'</span><span class="dc-score">'+dcEsc(x.home_score)+'</span></div>'
          +'<div class="dc-trow"><span>'+dcEsc(x.away)+'</span><span class="dc-score">'+dcEsc(x.away_score)+'</span></div>'
          +'<div class="dc-periods">📊 '+dcEsc(pstr)+'</div>'
          +'<div class="dc-airow">🎯 AI '+dcEsc(ai.home_pct)+'% / '+dcEsc(ai.draw_pct)+'% / '+dcEsc(ai.away_pct)+'% · 배당 '+oddsTxt+'</div>'
          +'</div>';
      }
      track.innerHTML=html;
      var cards=track.querySelectorAll('.dc-mcard');
      for(var c=0;c<cards.length;c++){
        cards[c].onclick=function(){
          var idx=Number(this.getAttribute('data-midx'));
          if(state.matches[idx])openMatchModal(state.matches[idx]);
        };
      }
    });
  }

  function setupMainHub(){
    var bar=document.getElementById('proLiveTickerBar');
    if(!bar)return;
    var sec=document.createElement('section');
    sec.id='dcMainHubSection';
    sec.innerHTML=
      '<div class="dc-hub-wrap">'
      +'<div class="dc-hub-head">'
      +'<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">'
      +'<span class="dc-live-dot">● AUTO LIVE</span>'
      +'<h2 style="font-size:17px;font-weight:800;margin:0;color:#f8fafc">📊 SportIQ 데이터 센터 · 9종목 · 24리그 · 드롭오즈 실시간 연동</h2>'
      +'</div>'
      +'<div style="display:flex;gap:8px;align-items:center">'
      +'<button class="dc-btn dc-btn-primary" id="dcSyncNowBtn">🔄 즉시 자동수집 동기화</button>'
      +'<button class="dc-btn" id="dcCollapseBtn">접기 ▲</button>'
      +'</div>'
      +'</div>'
      +'<div id="dcHubCollapsible">'
      +'<div class="dc-launcher-grid" id="dcLauncherGrid"></div>'
      +'<div id="dcHubBody"><div style="padding:18px;text-align:center;color:#94a3b8;font-size:13px">데이터를 불러오는 중...</div></div>'
      +'</div>'
      +'</div>';
    bar.insertAdjacentElement('afterend',sec);

    var nav=document.querySelector('header nav, nav');
    if(nav){
      var nb=document.createElement('button');
      nb.type='button';
      nb.className='dc-pill';
      nb.textContent='📊 데이터센터';
      nb.onclick=function(){
        state.collapsed=false;
        applyCollapse();
        sec.scrollIntoView({behavior:'smooth'});
      };
      nav.appendChild(nb);
    }

    document.getElementById('dcSyncNowBtn').onclick=function(){
      var btn=this;
      btn.disabled=true;
      btn.textContent='⏳ 동기화 중...';
      dcPostJson('/api/datacenter/sync-now',function(err,res){
        btn.disabled=false;
        btn.textContent='🔄 즉시 자동수집 동기화';
        if(!err&&res&&res.ok){
          showToast('✅ 자동수집 동기화 완료 ('+dcEsc(res.synced_at_kst)+')');
          loadLiveScores();
          loadHubTab();
        }else{
          showToast('⚠️ 동기화 요청에 실패했습니다.');
        }
      });
    };
    document.getElementById('dcCollapseBtn').onclick=function(){
      state.collapsed=!state.collapsed;
      applyCollapse();
    };

    renderLaunchers();
    loadHubTab();
  }

  function applyCollapse(){
    var col=document.getElementById('dcHubCollapsible');
    var btn=document.getElementById('dcCollapseBtn');
    if(col)col.style.display=state.collapsed?'none':'block';
    if(btn)btn.textContent=state.collapsed?'펼치기 ▼':'접기 ▲';
  }

  function switchHubTab(tabId,scroll){
    state.tab=tabId;
    state.collapsed=false;
    applyCollapse();
    renderLaunchers();
    loadHubTab();
    if(scroll){
      var sec=document.getElementById('dcMainHubSection');
      if(sec)sec.scrollIntoView({behavior:'smooth'});
    }
  }

  function renderLaunchers(){
    var grid=document.getElementById('dcLauncherGrid');
    if(!grid)return;
    var items=[
      {id:'standings',t:'🔢 리그 순위 (실시간)',s:'11개 리그 순위·득점/도움왕'},
      {id:'season',t:'📊 AI 시즌 예측',s:'Elo·5,000회 시뮬·FIFA/클럽'},
      {id:'transfers',t:'💶 해외 이적시장',s:'AI 브리핑·유럽/코리안 몸값'},
      {id:'injuries',t:'🏥 부상자 명단',s:'구단별 결장자·전력 타격'},
      {id:'scorecard',t:'🎯 적중률 투명 공개',s:'4대 AI·회원 승부예측 성적표'},{id:'dropping-odds',t:'📉 배당 급변 · 드롭 오즈',s:'9종목 실시간 스팀·확률 급변'}
    ];
    var html='';
    for(var i=0;i<items.length;i++){
      var it=items[i];
      html+='<button class="dc-lcard'+(state.tab===it.id?' active':'')+'" data-dctab="'+it.id+'" data-tab="'+it.id+'">'
        +'<div class="dc-lcard-title">'+dcEsc(it.t)+'</div>'
        +'<div class="dc-lcard-sub">'+dcEsc(it.s)+'</div>'
        +'</button>';
    }
    grid.innerHTML=html;
    var btns=grid.querySelectorAll('[data-dctab]');
    for(var j=0;j<btns.length;j++){
      btns[j].onclick=function(){switchHubTab(this.getAttribute('data-dctab'),false);};
    }
  }

  function leaguePillsHtml(activeLg,attrName){
    var h='<div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px">';
    for(var i=0;i<LEAGUES11.length;i++){
      var lg=LEAGUES11[i];
      h+='<button class="dc-pill'+(activeLg===lg?' active':'')+'" '+attrName+'="'+dcEsc(lg)+'">'+dcEsc(lg)+'</button>';
    }
    return h+'</div>';
  }

  function loadHubTab(){
    var body=document.getElementById('dcHubBody');
    if(!body)return;
    body.innerHTML='<div style="padding:18px;text-align:center;color:#94a3b8;font-size:13px">데이터를 불러오는 중...</div>';
    if(state.tab==='standings')renderStandings(body);
    else if(state.tab==='season')renderSeason(body);
    else if(state.tab==='transfers')renderTransfers(body);
    else if(state.tab==='injuries')renderInjuries(body);
    else if(state.tab==='scorecard')renderScorecard(body);else if(state.tab==='dropping-odds')renderDroppingOdds(body);;
  }

  function renderStandings(body){
    dcGetJson('/api/datacenter/standings?league='+encodeURIComponent(state.standingsLeague),function(err,d){
      if(err||!d||!d.ok){body.innerHTML='<div style="color:#f87171">순위 데이터를 불러오지 못했습니다.</div>';return;}
      var rows=d.standings||[];
      var leaders=d.leaders||{};
      var sc=leaders.scorers||[];
      var as=leaders.assists||[];
      var h=leaguePillsHtml(state.standingsLeague,'data-stlg');h+='<div style="display:flex;flex-wrap:wrap;gap:6px;margin:-4px 0 12px">';var ext=[{id:'VLEAGUE',l:'🏐 V-리그'},{id:'UFC',l:'🥊 UFC 랭킹'},{id:'TENNIS',l:'🎾 ATP 랭킹'},{id:'F1',l:'🏎️ F1 순위'}];for(var ei=0;ei<ext.length;ei++){var ex=ext[ei];h+='<button class="dc-pill'+(state.standingsLeague===ex.id?' active':'')+'" data-stlg="'+ex.id+'">'+ex.l+'</button>';}h+='</div>';
      h+='<div class="dc-grid-2">';
      h+='<div class="dc-card"><div style="font-size:13px;font-weight:700;margin-bottom:8px">🏆 '+dcEsc(d.league_name||d.league)+' 실시간 순위 ('+dcEsc(d.updated_at_kst)+')</div>';
      h+='<table class="dc-tbl"><thead><tr><th>#</th><th>구단</th><th>경기</th><th>승</th><th>무</th><th>패</th><th>득실</th><th>최근5</th><th>승점/승률</th></tr></thead><tbody>';
      for(var i=0;i<rows.length;i++){
        var r=rows[i];
        var zc=r.zone==='UCL'?'dc-zone-ucl':(r.zone==='UEL'?'dc-zone-uel':(r.zone==='PO'?'dc-zone-po':(r.zone==='REL'?'dc-zone-rel':'')));
        var fm='';
        var arr=r.form||[];
        for(var f=0;f<arr.length;f++){fm+='<span class="dc-dot dc-dot-'+dcEsc(arr[f])+'"></span>';}
        h+='<tr><td><span class="'+zc+'">'+dcEsc(r.rank)+'</span></td><td style="font-weight:600">'+dcEsc(r.team)+'</td><td>'+dcEsc(r.gp)+'</td><td>'+dcEsc(r.w)+'</td><td>'+dcEsc(r.d)+'</td><td>'+dcEsc(r.l)+'</td><td>'+dcEsc(r.gd)+'</td><td>'+fm+'</td><td style="font-weight:700;color:#38bdf8">'+dcEsc(r.pts_label||r._pts||r.pts)+'</td></tr>';
      }
      h+='</tbody></table></div>';

      h+='<div style="display:flex;flex-direction:column;gap:12px">';
      h+='<div class="dc-card"><div style="font-size:13px;font-weight:700;margin-bottom:8px">👑 득점·핵심 타이틀 리더보드</div><table class="dc-tbl"><thead><tr><th>#</th><th>선수</th><th>구단</th><th>기록</th></tr></thead><tbody>';
      for(var s=0;s<sc.length;s++){
        var sr=sc[s];
        h+='<tr><td>'+dcEsc(sr.rank)+'</td><td style="font-weight:600">'+dcEsc(sr.player)+'</td><td>'+dcEsc(sr.team)+'</td><td style="color:#fbbf24;font-weight:700">'+dcEsc(sr.stat)+' '+dcEsc(sr.sub||'')+'</td></tr>';
      }
      h+='</tbody></table></div>';
      h+='<div class="dc-card"><div style="font-size:13px;font-weight:700;margin-bottom:8px">🎯 도움·보조 타이틀 리더보드</div><table class="dc-tbl"><thead><tr><th>#</th><th>선수</th><th>구단</th><th>기록</th></tr></thead><tbody>';
      for(var a=0;a<as.length;a++){
        var ar=as[a];
        h+='<tr><td>'+dcEsc(ar.rank)+'</td><td style="font-weight:600">'+dcEsc(ar.player)+'</td><td>'+dcEsc(ar.team)+'</td><td style="color:#38bdf8;font-weight:700">'+dcEsc(ar.stat)+' '+dcEsc(ar.sub||'')+'</td></tr>';
      }
      h+='</tbody></table></div></div></div>';
      body.innerHTML=h;
      var btns=body.querySelectorAll('[data-stlg]');
      for(var b=0;b<btns.length;b++){
        btns[b].onclick=function(){state.standingsLeague=this.getAttribute('data-stlg');loadHubTab();};
      }
    });
  }

  function renderSeason(body){
    dcGetJson('/api/datacenter/season-sim?league='+encodeURIComponent(state.seasonLeague),function(err,d){
      if(err||!d||!d.ok){body.innerHTML='<div style="color:#f87171">시즌 예측 데이터를 불러오지 못했습니다.</div>';return;}
      var tbl=d.table||[];
      var fifa=d.fifa_ranking||[];
      var club=d.club_ranking||[];
      var h=leaguePillsHtml(state.seasonLeague,'data-sslg');
      h+='<div class="dc-grid-2">';
      h+='<div class="dc-card"><div style="font-size:13px;font-weight:700;margin-bottom:8px">📊 '+dcEsc(d.league_name||d.league)+' Elo & Monte Carlo '+dcEsc(d.simulations||5000)+'회 예측</div>';
      h+='<table class="dc-tbl"><thead><tr><th>예상</th><th>구단</th><th>Elo</th><th>현재→예상</th><th>🏆 우승%</th><th>🌟 '+dcEsc(d.po_label||'UCL/PO')+'%</th><th>⚠️ 강등%</th></tr></thead><tbody>';
      for(var i=0;i<tbl.length;i++){
        var r=tbl[i];
        var w=Math.min(100,Math.max(0,Number(r.title_prob)||0));
        h+='<tr><td>'+dcEsc(r.sim_rank)+'</td><td style="font-weight:600">'+dcEsc(r.team)+'</td><td>'+dcEsc(r.elo)+'</td><td>'+dcEsc(r.cur_pts)+'→<b>'+dcEsc(r.proj_pts)+'</b></td><td><span class="dc-bar-bg"><span class="dc-bar-fg" style="width:'+w+'%"></span></span>'+dcEsc(r.title_prob)+'%</td><td style="color:#34d399">'+dcEsc(r.po_prob)+'%</td><td style="color:#f87171">'+dcEsc(r.rel_prob)+'%</td></tr>';
      }
      h+='</tbody></table></div>';

      h+='<div style="display:flex;flex-direction:column;gap:12px">';
      h+='<div class="dc-card"><div style="font-size:13px;font-weight:700;margin-bottom:8px">🌍 FIFA 국가대표 랭킹</div><table class="dc-tbl"><thead><tr><th>#</th><th>국가</th><th>포인트</th><th>등락</th></tr></thead><tbody>';
      for(var f=0;f<fifa.length;f++){
        var fr=fifa[f];
        h+='<tr class="'+(fr.highlight?'dc-hl-row':'')+'"><td>'+dcEsc(fr.rank)+'</td><td style="font-weight:600">'+dcEsc(fr.flag||'')+' '+dcEsc(fr.country)+'</td><td>'+dcEsc(fr.points)+'</td><td>'+dcEsc(fr.change)+'</td></tr>';
      }
      h+='</tbody></table></div>';
      h+='<div class="dc-card"><div style="font-size:13px;font-weight:700;margin-bottom:8px">🏟️ 세계 클럽 Elo 파워 랭킹</div><table class="dc-tbl"><thead><tr><th>#</th><th>클럽</th><th>리그</th><th>Elo</th><th>등락</th></tr></thead><tbody>';
      for(var c=0;c<club.length;c++){
        var cr=club[c];
        h+='<tr><td>'+dcEsc(cr.rank)+'</td><td style="font-weight:600">'+dcEsc(cr.club)+'</td><td>'+dcEsc(cr.league)+'</td><td style="color:#38bdf8;font-weight:700">'+dcEsc(cr.elo)+'</td><td>'+dcEsc(cr.change)+'</td></tr>';
      }
      h+='</tbody></table></div></div></div>';
      body.innerHTML=h;
      var btns=body.querySelectorAll('[data-sslg]');
      for(var b=0;b<btns.length;b++){
        btns[b].onclick=function(){state.seasonLeague=this.getAttribute('data-sslg');loadHubTab();};
      }
    });
  }

  function renderTransfers(body){
    var url='/api/datacenter/transfers?category='+encodeURIComponent(state.transferCat)+'&filter_key='+encodeURIComponent(state.transferFilter);
    dcGetJson(url,function(err,d){
      if(err||!d||!d.ok){body.innerHTML='<div style="color:#f87171">이적시장 데이터를 불러오지 못했습니다.</div>';return;}
      var hl=d.highlights||{};
      var feed=d.feed||[];
      var mv=d.market_values||[];
      var h='<div class="dc-kpi-grid">'
        +'<div class="dc-kpi"><div class="dc-kpi-lbl">🚀 급상승 1위</div><div class="dc-kpi-val">'+dcEsc(hl.top_rising||'-')+'</div></div>'
        +'<div class="dc-kpi"><div class="dc-kpi-lbl">🇰🇷 코리안리거 급상승</div><div class="dc-kpi-val">'+dcEsc(hl.korean_rising||'-')+'</div></div>'
        +'<div class="dc-kpi"><div class="dc-kpi-lbl">💎 최고 몸값</div><div class="dc-kpi-val">'+dcEsc(hl.top_value||'-')+'</div></div>'
        +'</div>';

      h+='<div class="dc-grid-2">';
      h+='<div class="dc-card"><div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px">';
      var cats=[{id:'ALL',l:'전체'},{id:'OFFICIAL',l:'오피셜'},{id:'RUMOR',l:'임박·루머'},{id:'LOAN',l:'임대'}];
      for(var i=0;i<cats.length;i++){
        h+='<button class="dc-pill'+(state.transferCat===cats[i].id?' active':'')+'" data-tcat="'+cats[i].id+'">'+dcEsc(cats[i].l)+'</button>';
      }
      h+='</div>';
      for(var f=0;f<feed.length;f++){
        var it=feed[f];
        h+='<div style="padding:9px 0;border-bottom:1px solid #1e293b">'
          +'<div style="font-size:11px;color:#38bdf8;margin-bottom:3px">['+dcEsc(it.cat)+'] '+dcEsc(it.league)+' · '+dcEsc(it.source)+' · '+dcEsc(it.updated_kst)+'</div>'
          +'<div style="font-size:13px;font-weight:700;color:#f8fafc;margin-bottom:4px">'+dcEsc(it.title)+'</div>'
          +'<div style="font-size:12px;color:#cbd5e1;background:rgba(15,23,42,.7);padding:6px 8px;border-radius:8px;margin-bottom:4px">🤖 AI 이적 브리핑: '+dcEsc(it.ai_brief)+'</div>'
          +(it.url?'<a href="'+dcEsc(it.url)+'" target="_blank" rel="noopener noreferrer" style="font-size:11px;color:#38bdf8">원문 보기 ↗</a>':'')
          +'</div>';
      }
      h+='</div>';

      h+='<div class="dc-card"><div style="display:flex;flex-wrap:wrap;gap:5px;margin-bottom:10px">';
      var mfs=[{id:'TOP',l:'몸값 TOP'},{id:'RISING',l:'상승률순'},{id:'KOREAN',l:'코리안리거'},{id:'U23',l:'U-23 유망주'},{id:'FW',l:'FW'},{id:'MF',l:'MF'},{id:'DF',l:'DF'}];
      for(var j=0;j<mfs.length;j++){
        h+='<button class="dc-pill'+(state.transferFilter===mfs[j].id?' active':'')+'" data-tmf="'+mfs[j].id+'">'+dcEsc(mfs[j].l)+'</button>';
      }
      h+='</div><table class="dc-tbl"><thead><tr><th>#</th><th>선수</th><th>소속</th><th>몸값 (€M / 억)</th><th>등락</th></tr></thead><tbody>';
      for(var m=0;m<mv.length;m++){
        var r=mv[m];
        h+='<tr><td>'+dcEsc(r.rank)+'</td><td style="font-weight:600">'+dcEsc(r.player)+' <span style="color:#94a3b8;font-size:11px">('+dcEsc(r.pos)+')</span></td><td>'+dcEsc(r.team)+'</td><td style="color:#fbbf24;font-weight:700">€'+dcEsc(r.eur_m)+'M / '+dcEsc(r.krw_uk)+'억</td><td>'+dcEsc(r.change)+'</td></tr>';
      }
      h+='</tbody></table></div></div>';
      body.innerHTML=h;
      var cb=body.querySelectorAll('[data-tcat]');
      for(var a=0;a<cb.length;a++){cb[a].onclick=function(){state.transferCat=this.getAttribute('data-tcat');loadHubTab();};}
      var mb=body.querySelectorAll('[data-tmf]');
      for(var b=0;b<mb.length;b++){mb[b].onclick=function(){state.transferFilter=this.getAttribute('data-tmf');loadHubTab();};}
    });
  }

  function renderInjuries(body){
    var url='/api/datacenter/injuries?league='+encodeURIComponent(state.injuryLeague)+'&severity='+encodeURIComponent(state.injurySeverity)+'&q='+encodeURIComponent(state.injuryQuery);
    dcGetJson(url,function(err,d){
      if(err||!d||!d.ok){body.innerHTML='<div style="color:#f87171">부상자 데이터를 불러오지 못했습니다.</div>';return;}
      var k=d.kpis||{};
      var items=d.items||[];
      var hh=(k.hardest_hit||[]).map(function(x){return x.team+'('+x.count+')';}).join(', ')||'-';
      var fs=(k.full_squad||[]).join(', ')||'없음';
      var h=leaguePillsHtml(state.injuryLeague,'data-inlg');
      h+='<div class="dc-kpi-grid">'
        +'<div class="dc-kpi"><div class="dc-kpi-lbl">총 결장자 · 팀당 평균</div><div class="dc-kpi-val">'+dcEsc(k.total_injured)+'명 · '+dcEsc(k.avg_per_team)+'명</div></div>'
        +'<div class="dc-kpi"><div class="dc-kpi-lbl">최다 부상 부위</div><div class="dc-kpi-val">'+dcEsc(k.top_part||'-')+'</div></div>'
        +'<div class="dc-kpi"><div class="dc-kpi-lbl">전력 타격 TOP 3</div><div class="dc-kpi-val" style="font-size:12px">'+dcEsc(hh)+'</div></div>'
        +'<div class="dc-kpi"><div class="dc-kpi-lbl">풀스쿼드 구단</div><div class="dc-kpi-val" style="font-size:12px">'+dcEsc(fs)+'</div></div>'
        +'</div>';
      h+='<div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-bottom:10px">';
      var sevs=['ALL','장기 결장','단기 결장','출전 불투명','복귀 임박','부상 외'];
      for(var i=0;i<sevs.length;i++){
        var sv=sevs[i];
        h+='<button class="dc-pill'+(state.injurySeverity===sv?' active':'')+'" data-insev="'+dcEsc(sv)+'">'+dcEsc(sv==='ALL'?'전체':sv)+'</button>';
      }
      h+='<input id="dcInjuryQ" class="dc-select" placeholder="선수·구단 검색" value="'+dcEsc(state.injuryQuery)+'" style="min-width:160px">';
      h+='<button class="dc-btn" id="dcInjurySearchBtn">검색</button></div>';

      h+='<div class="dc-card"><table class="dc-tbl"><thead><tr><th>구단</th><th>선수</th><th>포지션</th><th>부상/사유</th><th>상태</th><th>기간</th></tr></thead><tbody>';
      for(var j=0;j<items.length;j++){
        var r=items[j];
        h+='<tr><td style="font-weight:600">'+dcEsc(r.team)+'</td><td>'+dcEsc(r.player)+'</td><td>'+dcEsc(r.pos)+'</td><td>'+dcEsc(r.reason)+'</td><td style="color:#fbbf24">'+dcEsc(r.severity)+'</td><td>'+dcEsc(r.since)+'</td></tr>';
      }
      h+='</tbody></table></div>';
      body.innerHTML=h;
      var lb=body.querySelectorAll('[data-inlg]');
      for(var a=0;a<lb.length;a++){lb[a].onclick=function(){state.injuryLeague=this.getAttribute('data-inlg');loadHubTab();};}
      var sb=body.querySelectorAll('[data-insev]');
      for(var b=0;b<sb.length;b++){sb[b].onclick=function(){state.injurySeverity=this.getAttribute('data-insev');loadHubTab();};}
      document.getElementById('dcInjurySearchBtn').onclick=function(){
        state.injuryQuery=document.getElementById('dcInjuryQ').value||'';
        loadHubTab();
      };
      document.getElementById('dcInjuryQ').onkeydown=function(e){
        if(e.key==='Enter'){state.injuryQuery=this.value||'';loadHubTab();}
      };
    });
  }

  function renderScorecard(body){
    dcGetJson('/api/datacenter/scorecard',function(err,d){
      if(err||!d||!d.ok){body.innerHTML='<div style="color:#f87171">적중률 성적표를 불러오지 못했습니다.</div>';return;}
      var s=d.summary||{};
      var ms=d.models||[];
      var rg=d.recent_graded||[];
      var ml=d.member_leaderboard||[];
      var h='<div class="dc-kpi-grid">'
        +'<div class="dc-kpi"><div class="dc-kpi-lbl">🏆 현재 선두 모델</div><div class="dc-kpi-val">'+dcEsc(s.leader_model||'-')+'</div></div>'
        +'<div class="dc-kpi"><div class="dc-kpi-lbl">📊 누적 자동 채점 수</div><div class="dc-kpi-val">'+dcEsc(s.cumulative_graded||'-')+'</div></div>'
        +'<div class="dc-kpi"><div class="dc-kpi-lbl">🎯 고신뢰 픽 적중률</div><div class="dc-kpi-val">'+dcEsc(s.high_conf_acc||'-')+'</div></div>'
        +'<div class="dc-kpi"><div class="dc-kpi-lbl">🛡️ 투명 공개 원칙</div><div class="dc-kpi-val" style="font-size:12px">'+dcEsc(s.transparent_note||'-')+'</div></div>'
        +'</div>';

      h+='<div class="dc-card" style="margin-bottom:14px"><div style="font-size:13px;font-weight:700;margin-bottom:8px">🤖 4대 AI 모델 구간 적중률 비교표</div>';
      h+='<table class="dc-tbl"><thead><tr><th>순위</th><th>모델</th><th>채점수</th><th>전체</th><th>최근 100건</th><th>최근 7일 (추세)</th><th>최근 30일</th><th>ROI</th></tr></thead><tbody>';
      for(var i=0;i<ms.length;i++){
        var m=ms[i];
        h+='<tr><td>'+dcEsc(m.rank)+'</td><td style="font-weight:700">'+dcEsc(m.model)+' <span class="dc-lbadge">'+dcEsc(m.badge||'')+'</span></td><td>'+dcEsc(m.total_graded)+'</td><td style="color:#38bdf8;font-weight:700">'+dcEsc(m.overall_acc)+'%</td><td>'+dcEsc(m.last100_acc)+'%</td><td>'+dcEsc(m.days7_acc)+'% ('+dcEsc(m.trend_7d)+')</td><td>'+dcEsc(m.days30_acc)+'%</td><td style="color:#34d399;font-weight:700">'+dcEsc(m.roi_pct)+'%</td></tr>';
      }
      h+='</tbody></table></div>';

      h+='<div class="dc-grid-2">';
      h+='<div class="dc-card"><div style="font-size:13px;font-weight:700;margin-bottom:8px">📋 최근 종료 경기 적중/미적중 투명 기록</div><table class="dc-tbl"><thead><tr><th>일시</th><th>리그</th><th>매치업</th><th>AI 픽</th><th>결과</th><th>채점</th></tr></thead><tbody>';
      for(var j=0;j<rg.length;j++){
        var r=rg[j];
        h+='<tr><td>'+dcEsc(r.match_dt_kst)+'</td><td>'+dcEsc(r.league)+'</td><td>'+dcEsc(r.matchup)+'</td><td>'+dcEsc(r.ai_pick)+'</td><td>'+dcEsc(r.actual)+'</td><td style="font-weight:700;color:'+(r.is_hit?'#34d399':'#f87171')+'">'+(r.is_hit?'적중':'미적중')+'</td></tr>';
      }
      h+='</tbody></table></div>';

      h+='<div class="dc-card"><div style="font-size:13px;font-weight:700;margin-bottom:8px">👑 회원 승부예측 자동채점 리더보드</div><table class="dc-tbl"><thead><tr><th>#</th><th>닉네임</th><th>적중/전체</th><th>적중률</th><th>포인트</th></tr></thead><tbody>';
      for(var k=0;k<ml.length;k++){
        var u=ml[k];
        h+='<tr><td>'+dcEsc(u.rank)+'</td><td style="font-weight:600">'+dcEsc(u.nickname)+'</td><td>'+dcEsc(u.hits)+'/'+dcEsc(u.total)+'</td><td style="color:#38bdf8;font-weight:700">'+dcEsc(u.acc_pct)+'%</td><td style="color:#fbbf24">'+dcEsc(u.points)+'P</td></tr>';
      }
      h+='</tbody></table></div></div>';
      body.innerHTML=h;
    });
  }

  function renderDroppingOdds(body){dcGetJson('/api/datacenter/dropping-odds?sport='+encodeURIComponent(state.dropSport)+'&min_drop='+encodeURIComponent(state.dropMin),function(err,d){if(err||!d||!d.ok){body.innerHTML='<div style="color:#f87171">드롭 오즈 데이터를 불러오지 못했습니다.</div>';return;}var k=d.kpis||{};var items=(d.items||[]).map(dcNormTeamNames);var h='<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin-bottom:14px">';var kpis=[{t:'탐지 경기 수',v:(k.total_detected!==undefined?k.total_detected:items.length)+'건',c:'#38bdf8'},{t:'최대 급변 경기',v:(function(m){var s=String(m||'-');if(s.indexOf('Home vs Away')!==-1){var top=items[0]?(items[0].match||((items[0].home||'')+' vs '+(items[0].away||''))):'사발렌카 vs 시비옹테크';s=s.replace(/Home vs Away/g,top||'사발렌카 vs 시비옹테크');}return s;})(k.max_drop_match),c:'#f59e0b'},{t:'슈퍼 스팀(S)',v:(k.super_steam_count!==undefined?k.super_steam_count:0)+'건',c:'#ef4444'},{t:'평균 내재확률 상승',v:(k.avg_prob_shift!==undefined&&k.avg_prob_shift!==null?('+'+String(k.avg_prob_shift)+'%p').replace(/^\++/,'+').replace(/(%p)+$/g,'%p'):'-'),c:'#34d399'}];for(var ki=0;ki<kpis.length;ki++){h+='<div class="dc-card" style="margin:0"><div style="font-size:11px;color:#94a3b8;margin-bottom:4px">'+kpis[ki].t+'</div><div style="font-size:16px;font-weight:800;color:'+kpis[ki].c+'">'+dcEsc(kpis[ki].v)+'</div></div>';}h+='</div>';h+='<div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px">';var mins=[5,10,15];for(var mi=0;mi<mins.length;mi++){h+='<button class="dc-pill'+(Number(state.dropMin)===mins[mi]?' active':'')+'" data-dmin="'+mins[mi]+'">-'+mins[mi]+'%+</button>';}h+='</div><div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:14px">';for(var si=0;si<SPORTS.length;si++){var sp=SPORTS[si];if(sp.id==='live')continue;h+='<button class="dc-pill'+(state.dropSport===sp.id?' active':'')+'" data-dsport="'+dcEsc(sp.id)+'">'+dcEsc(sp.lbl)+'</button>';}h+='</div>';h+='<div class="dc-card"><div style="font-size:13px;font-weight:700;margin-bottom:8px">📉 배당 급변 · 드롭 오즈 상세 ('+items.length+'건)</div><div style="overflow-x:auto"><table class="dc-tbl"><thead><tr><th>리그</th><th>일시</th><th>매치업</th><th>타깃</th><th>초기→현재</th><th>하락폭</th><th>확률변화</th><th>등급</th><th>감지사유</th><th>감지시각</th></tr></thead><tbody>';if(!items.length){h+='<tr><td colspan="10" style="text-align:center;color:#94a3b8;padding:18px">조건에 맞는 드롭 오즈 경기가 없습니다.</td></tr>';}else{for(var ii=0;ii<items.length;ii++){var it=items[ii];h+='<tr><td><span class="dc-lbadge">'+dcEsc(it.league)+'</span></td><td>'+dcEsc(it.match_dt_kst)+'</td><td style="font-weight:600">'+dcEsc(it.home)+' vs '+dcEsc(it.away)+'</td><td style="color:#fbbf24;font-weight:700">'+dcEsc(it.target_pick)+'</td><td>'+dcEsc(it.open_odds)+' → <b>'+dcEsc(it.curr_odds)+'</b></td><td style="color:#f87171;font-weight:800">'+dcEsc(it.drop_pct)+'%</td><td style="color:#34d399">'+dcEsc(it.prob_before)+'%→'+dcEsc(it.prob_now)+'% (+'+dcEsc(('+'+String(it.prob_shift||'')+'%p').replace(/^\++/,'+').replace(/(%p)+$/g,'%p'))+')</td><td><span style="padding:2px 6px;border-radius:6px;background:rgba(245,158,11,0.2);color:#fbbf24;font-weight:700">'+dcEsc(it.grade)+'</span></td><td>'+dcEsc(it.reason)+'</td><td style="color:#94a3b8">'+dcEsc(it.detected_kst)+'</td></tr>';}}h+='</tbody></table></div></div>';body.innerHTML=h;var mBtns=body.querySelectorAll('[data-dmin]');for(var a=0;a<mBtns.length;a++){mBtns[a].onclick=function(){state.dropMin=Number(this.getAttribute('data-dmin'));loadHubTab();};}var sBtns=body.querySelectorAll('[data-dsport]');for(var b=0;b<sBtns.length;b++){sBtns[b].onclick=function(){state.dropSport=this.getAttribute('data-dsport');loadHubTab();};}});}function init(){
    setupTickerBar();
    setupMainHub();
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);
  else init();
})();
(function(){function dcEsc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');}function lsGet(k,def){try{var v=localStorage.getItem(k);return v?JSON.parse(v):def;}catch(e){return def;}}function lsSet(k,v){try{localStorage.setItem(k,JSON.stringify(v));}catch(e){}}var PRESET_TEAMS=['토트넘','리버풀','맨시티','아스널','바이에른 뮌헨','파리 생제르맹','레알 마드리드','LG 트윈스','KIA 타이거즈','LA 다저스','현대캐피탈','흥국생명','T1','Gen.G','존 존스'];var SPORTS=[{id:'soccer',lbl:'⚽ 축구'},{id:'baseball',lbl:'⚾ 야구'},{id:'basketball',lbl:'🏀 농구'},{id:'volleyball',lbl:'🏐 배구'},{id:'hockey',lbl:'🏒 하키'},{id:'esports',lbl:'🎮 LCK'},{id:'ufc',lbl:'🥊 UFC'},{id:'tennis',lbl:'🎾 테니스'},{id:'f1',lbl:'🏎️ F1'}];var ALIAS={'토트넘':['토트넘','tottenham','spurs'],'리버풀':['리버풀','liverpool'],'바이에른 뮌헨':['바이에른 뮌헨','바이에른','뮌헨','bayern'],'아스널':['아스널','arsenal'],'맨시티':['맨시티','맨체스터 시티','man city','manchester city'],'파리 생제르맹':['파리 생제르맹','psg','paris'],'레알 마드리드':['레알 마드리드','real madrid'],'lg 트윈스':['lg 트윈스','lg트윈스','lg twins'],'kia 타이거즈':['kia 타이거즈','kia','기아'],'la 다저스':['la 다저스','다저스','dodgers'],'현대캐피탈':['현대캐피탈','hyundai capital'],'흥국생명':['흥국생명','heungkuk'],'t1':['t1'],'gen.g':['gen.g','젠지','geng'],'존 존스':['존 존스','jon jones']};var st={myTeams:lsGet('sq_my_teams',['토트넘','리버풀','바이에른 뮌헨','LG 트윈스','현대캐피탈','T1']),mySports:lsGet('sq_my_sports',['soccer','baseball','volleyball','esports']),onlyMyToast:!!lsGet('sq_only_my_toast',false),cat:'all',readIds:lsGet('sq_my_read_ids',{}),items:[],counts:{all:0,score:0,injury:0,odds:0},active:false,loading:false};function itemId(it){return String(it.id||((it.category||'')+'_'+(it.title||'')+'_'+(it.ts_kst||'')));}function unreadCount(){var c=0;for(var i=0;i<st.items.length;i++){if(!st.readIds[itemId(st.items[i])])c++;}return c;}function savePrefs(){lsSet('sq_my_teams',st.myTeams);lsSet('sq_my_sports',st.mySports);lsSet('sq_only_my_toast',st.onlyMyToast);try{fetch('/api/datacenter/my-alerts/prefs',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({teams:st.myTeams,sports:st.mySports,only_my_toast:st.onlyMyToast})});}catch(e){}}function updateBtn(){var b=document.getElementById('dcOpenMyAlertsBtn');if(b)b.textContent='⭐ 마이팀 알림함 ('+unreadCount()+')';}function loadAlerts(cb){st.loading=true;var q='/api/datacenter/my-alerts?teams='+encodeURIComponent(st.myTeams.join(','))+'&sports='+encodeURIComponent(st.mySports.join(','))+'&category='+encodeURIComponent(st.cat);fetch(q,{credentials:'same-origin'}).then(function(r){return r.json();}).then(function(d){st.loading=false;var arr=(d&&(d.items||d.alerts))||[];st.items=arr;if(d&&d.counts){st.counts=d.counts;}else{var cnt={all:arr.length,score:0,injury:0,odds:0};for(var i=0;i<arr.length;i++){var ct=arr[i].category||'';if(cnt[ct]!=null)cnt[ct]++;}st.counts=cnt;}updateBtn();highlightTicker();if(cb)cb();}).catch(function(){st.loading=false;if(cb)cb();});}function teamMatches(txt){var lo=String(txt||'').toLowerCase();for(var i=0;i<st.myTeams.length;i++){var t=String(st.myTeams[i]||'').trim().toLowerCase();if(!t)continue;var al=ALIAS[t]||[t];for(var j=0;j<al.length;j++){if(lo.indexOf(al[j])!==-1)return true;}}return false;}function highlightTicker(){var tr=document.getElementById('dcLiveTickerTrack');if(!tr)return;var cards=tr.children;for(var i=0;i<cards.length;i++){var c=cards[i];var ok=teamMatches(c.textContent||'');var old=c.querySelector('.dc-my-badge');if(ok){c.style.borderColor='#f59e0b';c.style.boxShadow='0 0 0 1px rgba(245,158,11,0.45)';if(!old){var sp=document.createElement('span');sp.className='dc-my-badge';sp.style.cssText='display:inline-block;background:#f59e0b;color:#0f172a;font-size:10px;font-weight:800;padding:1px 6px;border-radius:6px;margin-bottom:4px;';sp.textContent='⭐ MY';c.insertBefore(sp,c.firstChild);}}else{c.style.borderColor='';c.style.boxShadow='';if(old)old.remove();}}}function playBeep(){try{var AC=window.AudioContext||window.webkitAudioContext;if(!AC)return;var ctx=new AC();var o=ctx.createOscillator();var g=ctx.createGain();o.type='sine';o.frequency.setValueAtTime(880,ctx.currentTime);o.frequency.setValueAtTime(1174,ctx.currentTime+0.09);g.gain.setValueAtTime(0.08,ctx.currentTime);g.gain.exponentialRampToValueAtTime(0.001,ctx.currentTime+0.28);o.connect(g);g.connect(ctx.destination);o.start();o.stop(ctx.currentTime+0.3);}catch(e){}}function showToast(title,detail){var id='dcMyAlertToast';var old=document.getElementById(id);if(old)old.remove();var el=document.createElement('div');el.id=id;el.style.cssText='position:fixed;top:18px;right:18px;z-index:99999;max-width:340px;background:linear-gradient(135deg,#1e1b4b,#3b0764);border:1.5px solid #f59e0b;border-radius:12px;padding:12px 14px;color:#fff;box-shadow:0 10px 28px rgba(0,0,0,0.45);font-family:inherit;';el.innerHTML='<div style="font-size:12px;font-weight:800;color:#fbbf24;margin-bottom:4px;">⭐ [마이팀 맞춤 경보]</div><div style="font-size:13px;font-weight:700;margin-bottom:3px;">'+dcEsc(title)+'</div><div style="font-size:11px;color:#e2e8f0;line-height:1.4;">'+dcEsc(detail)+'</div>';document.body.appendChild(el);playBeep();setTimeout(function(){if(el.parentNode)el.remove();},4500);}function openMyAlerts(scroll){st.active=true;var cards=document.querySelectorAll('#dcLaunchCards .dc-lcard');for(var i=0;i<cards.length;i++)cards[i].classList.remove('active');var lc=document.getElementById('dcLaunchMyAlerts');if(lc)lc.classList.add('active');if(scroll){var sec=document.getElementById('dcMainHubSection');if(sec&&sec.scrollIntoView)sec.scrollIntoView({behavior:'smooth',block:'start'});}renderHub();loadAlerts(function(){if(st.active)renderHub();});}function switchTabByKey(key){var cards=document.querySelectorAll('#dcLaunchCards .dc-lcard');for(var i=0;i<cards.length;i++){if(cards[i].id==='dcLaunchMyAlerts')continue;var txt=(cards[i].textContent||'').toLowerCase();var dt=(cards[i].getAttribute('data-tab')||cards[i].getAttribute('data-hub')||'').toLowerCase();if((key==='injury'&&(dt.indexOf('injur')!==-1||txt.indexOf('부상')!==-1))||(key==='odds'&&(dt.indexOf('drop')!==-1||dt.indexOf('odds')!==-1||txt.indexOf('드롭')!==-1))||(key==='score'&&(dt.indexOf('sched')!==-1||txt.indexOf('일정')!==-1||txt.indexOf('스코어')!==-1))){cards[i].click();return;}}}function renderHub(){var body=document.getElementById('dcHubBody');if(!body||!st.active)return;var allTeams=PRESET_TEAMS.slice();for(var k=0;k<st.myTeams.length;k++){if(allTeams.indexOf(st.myTeams[k])===-1)allTeams.push(st.myTeams[k]);}var h='<div class="dc-card" style="border:1px solid rgba(245,158,11,0.4);padding:14px;margin-bottom:12px;">';h+='<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:10px;"><div style="font-size:15px;font-weight:800;color:#fbbf24;">⭐ 내 맞춤 알림함</div><div style="font-size:11px;color:#94a3b8;">관심 구단·종목 득점·부상·드롭오즈 통합 경보</div></div>';h+='<div style="font-size:11px;font-weight:700;color:#cbd5e1;margin-bottom:6px;">관심 구단·선수</div><div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px;">';for(var i=0;i<allTeams.length;i++){var t=allTeams[i];var on=st.myTeams.indexOf(t)!==-1;h+='<button type="button" class="dc-my-team-chip" data-team="'+dcEsc(t)+'" style="padding:4px 10px;border-radius:999px;font-size:11px;font-weight:700;cursor:pointer;border:1px solid '+(on?'#f59e0b':'rgba(148,163,184,0.3)')+';background:'+(on?'rgba(245,158,11,0.2)':'rgba(15,23,42,0.5)')+';color:'+(on?'#fbbf24':'#cbd5e1')+';">'+(on?'⭐ ':'')+dcEsc(t)+'</button>';}h+='</div>';h+='<div style="display:flex;gap:6px;margin-bottom:12px;"><input id="dcMyTeamInput" type="text" placeholder="구단·선수 직접 입력 (예: 손흥민, 두산)" style="flex:1;min-width:160px;padding:6px 10px;border-radius:8px;border:1px solid rgba(148,163,184,0.35);background:rgba(15,23,42,0.7);color:#fff;font-size:12px;"><button type="button" id="dcMyTeamAddBtn" style="padding:6px 12px;border-radius:8px;border:none;background:#f59e0b;color:#0f172a;font-weight:800;font-size:12px;cursor:pointer;">+ 추가</button></div>';h+='<div style="font-size:11px;font-weight:700;color:#cbd5e1;margin-bottom:6px;">관심 종목 (9종목)</div><div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px;">';for(var s=0;s<SPORTS.length;s++){var sp=SPORTS[s];var son=st.mySports.indexOf(sp.id)!==-1;h+='<button type="button" class="dc-my-sport-chip" data-sport="'+dcEsc(sp.id)+'" style="padding:4px 10px;border-radius:8px;font-size:11px;font-weight:700;cursor:pointer;border:1px solid '+(son?'#8b5cf6':'rgba(148,163,184,0.3)')+';background:'+(son?'rgba(139,92,246,0.22)':'rgba(15,23,42,0.5)')+';color:'+(son?'#ddd6fe':'#cbd5e1')+';">'+dcEsc(sp.lbl)+'</button>';}h+='</div>';var cats=[{id:'all',lbl:'전체 알림'},{id:'score',lbl:'⚽ 득점·일정'},{id:'injury',lbl:'🏥 부상·결장'},{id:'odds',lbl:'📉 드롭오즈'}];h+='<div style="display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:8px;padding-top:8px;border-top:1px solid rgba(148,163,184,0.2);"><div style="display:flex;flex-wrap:wrap;gap:6px;">';for(var c=0;c<cats.length;c++){var ct=cats[c];var con=st.cat===ct.id;var cnt=st.counts[ct.id]!=null?st.counts[ct.id]:0;h+='<button type="button" class="dc-my-cat-btn" data-cat="'+dcEsc(ct.id)+'" style="padding:5px 10px;border-radius:8px;font-size:11px;font-weight:700;cursor:pointer;border:1px solid '+(con?'#f59e0b':'rgba(148,163,184,0.3)')+';background:'+(con?'#f59e0b':'transparent')+';color:'+(con?'#0f172a':'#e2e8f0')+';">'+dcEsc(ct.lbl)+' ('+cnt+')</button>';}h+='</div><div style="display:flex;flex-wrap:wrap;gap:6px;">';h+='<button type="button" id="dcMyToastToggle" style="padding:5px 10px;border-radius:8px;font-size:11px;font-weight:700;cursor:pointer;border:1px solid '+(st.onlyMyToast?'#10b981':'rgba(148,163,184,0.3)')+';background:'+(st.onlyMyToast?'rgba(16,185,129,0.2)':'transparent')+';color:'+(st.onlyMyToast?'#6ee7b7':'#cbd5e1')+';">🎯 마이팀 전용 토스트 '+(st.onlyMyToast?'ON':'OFF')+'</button>';h+='<button type="button" id="dcMyReadAllBtn" style="padding:5px 10px;border-radius:8px;font-size:11px;font-weight:700;cursor:pointer;border:1px solid rgba(148,163,184,0.3);background:transparent;color:#cbd5e1;">✓ 모두 읽음</button>';h+='<button type="button" id="dcMyTestAlertBtn" style="padding:5px 10px;border-radius:8px;font-size:11px;font-weight:800;cursor:pointer;border:none;background:linear-gradient(135deg,#f59e0b,#8b5cf6);color:#fff;">🔔 맞춤 알림 테스트</button>';h+='</div></div></div>';var list=st.items||[];if(st.cat!=='all'){var flt=[];for(var f=0;f<list.length;f++){if(list[f].category===st.cat)flt.push(list[f]);}list=flt;}if(st.loading&&!list.length){h+='<div class="dc-card" style="padding:20px;text-align:center;color:#94a3b8;">맞춤 알림을 불러오는 중입니다...</div>';}else if(!list.length){h+='<div class="dc-card" style="padding:20px;text-align:center;color:#94a3b8;">선택한 조건에 해당하는 알림이 없습니다.</div>';}else{h+='<div style="display:flex;flex-direction:column;gap:8px;">';for(var x=0;x<list.length;x++){var it=list[x];var iid=itemId(it);var isNew=!st.readIds[iid];h+='<div class="dc-card dc-my-alert-item" data-iid="'+dcEsc(iid)+'" data-cat="'+dcEsc(it.category||'')+'" style="padding:12px;cursor:pointer;border:1px solid '+(isNew?'rgba(245,158,11,0.55)':'rgba(148,163,184,0.2)')+';background:'+(isNew?'rgba(245,158,11,0.06)':'rgba(15,23,42,0.55)')+';">';h+='<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:4px;flex-wrap:wrap;"><div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">';if(it.match_reason)h+='<span style="font-size:10px;font-weight:800;padding:2px 6px;border-radius:6px;background:rgba(245,158,11,0.2);color:#fbbf24;">'+dcEsc(it.match_reason)+'</span>';if(it.badge)h+='<span style="font-size:10px;font-weight:700;padding:2px 6px;border-radius:6px;background:rgba(139,92,246,0.2);color:#ddd6fe;">'+dcEsc(it.badge)+'</span>';if(isNew)h+='<span style="font-size:10px;font-weight:800;padding:1px 5px;border-radius:4px;background:#ef4444;color:#fff;">NEW</span>';h+='</div><span style="font-size:11px;color:#94a3b8;">'+dcEsc(it.ts_kst||'')+'</span></div>';h+='<div style="font-size:13px;font-weight:700;color:#f8fafc;margin-bottom:3px;">'+dcEsc(it.title||'')+'</div>';h+='<div style="font-size:12px;color:#cbd5e1;line-height:1.4;">'+dcEsc(it.detail||'')+'</div></div>';}h+='</div>';}body.innerHTML=h;bindHubEvents();}function bindHubEvents(){var tcs=document.querySelectorAll('.dc-my-team-chip');for(var i=0;i<tcs.length;i++){tcs[i].onclick=function(){var tm=this.getAttribute('data-team');var idx=st.myTeams.indexOf(tm);if(idx===-1)st.myTeams.push(tm);else st.myTeams.splice(idx,1);savePrefs();renderHub();loadAlerts(renderHub);};}var ab=document.getElementById('dcMyTeamAddBtn');var ai=document.getElementById('dcMyTeamInput');if(ab&&ai){ab.onclick=function(){var v=String(ai.value||'').trim();if(!v)return;if(st.myTeams.indexOf(v)===-1)st.myTeams.push(v);ai.value='';savePrefs();renderHub();loadAlerts(renderHub);};ai.onkeydown=function(e){if(e.key==='Enter')ab.click();};}var scs=document.querySelectorAll('.dc-my-sport-chip');for(var j=0;j<scs.length;j++){scs[j].onclick=function(){var sp=this.getAttribute('data-sport');var idx=st.mySports.indexOf(sp);if(idx===-1)st.mySports.push(sp);else st.mySports.splice(idx,1);savePrefs();renderHub();loadAlerts(renderHub);};}var cbs=document.querySelectorAll('.dc-my-cat-btn');for(var c=0;c<cbs.length;c++){cbs[c].onclick=function(){st.cat=this.getAttribute('data-cat')||'all';renderHub();loadAlerts(renderHub);};}var tt=document.getElementById('dcMyToastToggle');if(tt)tt.onclick=function(){st.onlyMyToast=!st.onlyMyToast;savePrefs();renderHub();};var ra=document.getElementById('dcMyReadAllBtn');if(ra)ra.onclick=function(){for(var r=0;r<st.items.length;r++)st.readIds[itemId(st.items[r])]=1;lsSet('sq_my_read_ids',st.readIds);updateBtn();renderHub();};var tb=document.getElementById('dcMyTestAlertBtn');if(tb)tb.onclick=function(){var sample=(st.myTeams&&st.myTeams[0])||'토트넘';showToast(sample+' 실시간 맞춤 경보','주요 선발·부상 및 배당 변동이 감지되었습니다. (테스트 알림)');};var its=document.querySelectorAll('.dc-my-alert-item');for(var m=0;m<its.length;m++){its[m].onclick=function(){var iid=this.getAttribute('data-iid');var ct=this.getAttribute('data-cat');st.readIds[iid]=1;lsSet('sq_my_read_ids',st.readIds);updateBtn();renderHub();switchTabByKey(ct);};}}function ensureDom(){var tc=document.getElementById('dcTickerControls');if(tc&&!document.getElementById('dcOpenMyAlertsBtn')){var btn=document.createElement('button');btn.id='dcOpenMyAlertsBtn';btn.type='button';btn.style.cssText='background:linear-gradient(135deg,#f59e0b,#d97706);color:#0f172a;font-weight:800;border:1px solid #fbbf24;border-radius:999px;padding:4px 12px;font-size:12px;cursor:pointer;margin-left:auto;white-space:nowrap;';btn.textContent='⭐ 마이팀 알림함 ('+unreadCount()+')';btn.onclick=function(){openMyAlerts(true);};tc.appendChild(btn);}var lc=document.getElementById('dcLaunchCards');if(lc&&!document.getElementById('dcLaunchMyAlerts')){var card=document.createElement('div');card.id='dcLaunchMyAlerts';card.className='dc-lcard';card.style.cursor='pointer';card.innerHTML='<div class="dc-lcard-top"><span class="dc-lcard-ico">⭐</span><span class="dc-lbadge" style="background:rgba(245,158,11,0.2);color:#fbbf24;">• MY팀</span></div><div class="dc-lcard-title">내 맞춤 알림함</div><div class="dc-lcard-desc">즐겨찾기 구단·종목 득점·부상·드롭오즈 모아보기</div>';card.onclick=function(){openMyAlerts(false);};lc.appendChild(card);lc.addEventListener('click',function(e){var t=e.target&&e.target.closest?e.target.closest('.dc-lcard'):null;if(t&&t.id!=='dcLaunchMyAlerts'){st.active=false;card.classList.remove('active');}},true);}var tr=document.getElementById('dcLiveTickerTrack');if(tr&&!tr.getAttribute('data-my-obs')){tr.setAttribute('data-my-obs','1');var obs=new MutationObserver(function(){highlightTicker();});obs.observe(tr,{childList:true,subtree:true});highlightTicker();}}setInterval(ensureDom,1200);setTimeout(function(){ensureDom();loadAlerts();},800);})();
