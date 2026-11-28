"""billnojay.com/privacy: 개인정보처리방침 as a real, crawlable page.

The old notice only lived in a footer modal (sportiq_lounge_router LEGAL_DOCS_V22), so
crawlers and people following a link never saw it. This page states what the service
actually does (2026-11-28 code): no sign-up, nickname + PBKDF2 hash of a 4-digit PIN
for picks and comments, partner inquiry contact details, IP used for rate limiting and
hashed for duplicate-like checks, browser storage for screen settings, Google Analytics
(signals and ad personalisation off), hosting on Render (Singapore) behind Cloudflare.

The privacy officer's contact comes from PRIVACY_CONTACT_EMAIL. Until the operator sets
it, the page says so instead of inventing an address.
"""
from __future__ import annotations

import html
import os

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["privacy"])

EFFECTIVE = "2026년 11월 28일"
PATH = "/privacy"


def _e(v: str) -> str:
    return html.escape(v, quote=True)


def contact_html() -> str:
    email = (os.environ.get("PRIVACY_CONTACT_EMAIL") or "").strip()
    if email:
        return f'<p>개인정보 보호 문의: <a href="mailto:{_e(email)}">{_e(email)}</a></p>'
    return ("<p>개인정보 보호책임자 연락처는 운영자가 확정하는 대로 이 자리에 적습니다. 그전까지는 홈 하단의 "
            "‘파트너·제휴 문의’ 창구에 ‘개인정보 문의’라고 적어 보내 주시면 확인 후 답변드립니다.</p>")


def body_html() -> str:
    return (
        f'<p class="muted">시행일 {EFFECTIVE}</p>'
        "<p>SportIQ(billnojay.com, 이하 ‘서비스’)는 회원가입 없이 쓰는 스포츠 정보·커뮤니티 서비스입니다. "
        "이 방침은 서비스가 실제로 처리하는 개인정보와 그 이유, 보관 기간, 이용자의 권리를 설명합니다.</p>"

        "<h2>1. 처리하는 개인정보</h2><ul>"
        "<li><strong>글·댓글·응원 작성 시</strong>: 이용자가 정한 닉네임, 작성한 내용, 삭제용 4자리 PIN의 단방향 해시값(PBKDF2). "
        "PIN 자체는 저장하지 않습니다.</li>"
        "<li><strong>포인트·칭호·북마크를 다른 기기와 연동할 때</strong>: 닉네임, 연동용 4자리 PIN의 단방향 해시값, 포인트·칭호·북마크 같은 활동 기록.</li>"
        "<li><strong>파트너·제휴 문의 시</strong>: 회사명, 담당자 이름, 연락처, 문의 내용.</li>"
        "<li><strong>서비스 이용 중 자동으로</strong>: IP 주소와 브라우저 정보(User-Agent), 접속 기록. "
        "IP는 짧은 시간 동안의 요청 횟수 제한에 쓰고, 공감·투표 중복 확인에는 IP를 되돌릴 수 없는 해시값으로 바꿔 씁니다.</li>"
        "<li><strong>브라우저 저장소(로컬 스토리지)</strong>: 화면 테마, 관심 팀, 즐겨찾기 같은 화면 설정. 이용자의 기기에만 저장됩니다.</li>"
        "<li><strong>방문 통계</strong>: Google Analytics 4가 쿠키로 페이지 조회, 기기·브라우저 종류, 대략적인 지역을 집계합니다. "
        "Google 신호와 광고 개인화는 꺼 두었습니다.</li></ul>"
        "<p>주민등록번호, 실명, 휴대전화번호, 이메일, 주소, 결제 정보는 받지 않습니다(제휴 문의에 이용자가 스스로 적은 연락처는 예외).</p>"

        "<h2>2. 처리 목적</h2><ul>"
        "<li>글·댓글을 게시하고, 작성자가 PIN으로 직접 삭제할 수 있게 하기 위해</li>"
        "<li>제휴 문의에 답하기 위해</li>"
        "<li>도배·자동 요청 같은 부정 이용을 막고 서비스를 안정적으로 운영하기 위해</li>"
        "<li>어떤 화면이 많이 쓰이는지 파악해 서비스를 개선하기 위해(방문 통계)</li></ul>"

        "<h2>3. 보관 기간과 파기</h2><ul>"
        "<li>글·댓글·응원: 작성자가 삭제하거나 운영 정책에 따라 삭제될 때까지. 삭제하면 공개 화면과 검색 결과용 페이지에서 바로 사라집니다.</li>"
        "<li>제휴 문의: 답변과 협의가 끝난 뒤 지체 없이 파기합니다.</li>"
        "<li>요청 횟수 제한용 IP: 서버 메모리에서만 쓰고 데이터베이스에 저장하지 않으며, 서버가 다시 시작되면 사라집니다.</li>"
        "<li>검색어 통계 기록: 생성일로부터 90일이 지나면 자동으로 삭제합니다.</li>"
        "<li>서버 접속 기록: 호스팅 업체(Render)가 정한 기간 동안 보관된 뒤 삭제됩니다.</li>"
        "<li>방문 통계: Google Analytics의 데이터 보관 기간 설정에 따릅니다.</li></ul>"
        "<p>파기할 때는 데이터베이스에서 복구할 수 없게 지웁니다.</p>"

        "<h2>4. 제3자 제공</h2>"
        "<p>이용자의 개인정보를 다른 회사나 사람에게 팔거나 제공하지 않습니다. 법령에 따라 수사기관 등이 정당한 절차로 요구하는 경우는 예외입니다.</p>"

        "<h2>5. 처리 위탁과 국외 이전</h2>"
        "<p>서비스 운영에 필요한 아래 업무는 외부 업체의 서버에서 이뤄지며, 이 과정에서 개인정보가 국외에 저장·처리됩니다.</p>"
        '<table><thead><tr><th>업체</th><th>국가</th><th>항목</th><th>목적</th><th>보관 기간</th></tr></thead><tbody>'
        "<tr><td>Render Services, Inc.</td><td>싱가포르(서버), 미국(본사)</td><td>1번의 모든 항목</td><td>웹 서버·데이터베이스 운영</td><td>3번의 기간</td></tr>"
        "<tr><td>Cloudflare, Inc.</td><td>미국 등(전 세계 네트워크)</td><td>IP 주소, 접속 기록</td><td>Render를 거치는 접속 전달·보안</td><td>업체 정책에 따름</td></tr>"
        "<tr><td>Google LLC</td><td>미국</td><td>방문 통계(쿠키, 기기·브라우저 정보, 대략적 지역)</td><td>Google Analytics 방문 통계</td><td>Analytics 보관 설정에 따름</td></tr>"
        "</tbody></table>"
        "<p>이전 시점과 방법: 이용자가 서비스에 접속하거나 글을 쓸 때 네트워크로 전송됩니다. 국외 이전을 원하지 않으면 서비스 이용을 멈추거나, "
        "방문 통계는 아래 6번의 방법으로 거부할 수 있습니다.</p>"

        "<h2>6. 쿠키와 방문 통계를 거부하는 방법</h2>"
        "<p>브라우저 설정에서 쿠키 저장을 막거나 지울 수 있습니다. Google Analytics는 "
        '<a href="https://tools.google.com/dlpage/gaoptout" rel="noopener" target="_blank">Google 애널리틱스 차단 브라우저 부가기능</a>'
        "으로도 거부할 수 있습니다. 쿠키를 막아도 서비스 이용에는 지장이 없습니다.</p>"

        "<h2>7. 이용자의 권리</h2>"
        "<p>이용자는 언제든 자신의 개인정보를 열람하거나 정정·삭제·처리 정지를 요구할 수 있습니다. "
        "직접 쓴 글과 댓글은 작성할 때 정한 PIN으로 바로 삭제할 수 있고, PIN을 잊었거나 다른 요청이 있으면 아래 9번 창구로 알려 주세요. "
        "닉네임만으로는 본인 확인이 어려우므로, 글 주소와 작성 시각 등을 함께 적어 주시면 확인이 빠릅니다.</p>"

        "<h2>8. 안전성 확보 조치</h2><ul>"
        "<li>삭제용 PIN은 단방향 해시(PBKDF2)로만 저장합니다.</li>"
        "<li>모든 접속은 HTTPS로 암호화합니다.</li>"
        "<li>관리 기능과 데이터베이스는 운영자만 접근할 수 있게 제한합니다.</li>"
        "<li>공개 화면에서는 제휴 문의 연락처를 가려서 보여 줍니다.</li></ul>"

        "<h2>9. 개인정보 보호책임자와 문의</h2>"
        + contact_html()
        + "<p>개인정보 침해에 대한 상담이 필요하면 개인정보침해신고센터(privacy.kisa.or.kr, 국번 없이 118), "
        "개인정보분쟁조정위원회(kopico.go.kr, 1833-6972)에도 문의할 수 있습니다.</p>"

        "<h2>10. 만 14세 미만 아동</h2>"
        "<p>서비스는 회원가입을 받지 않으며, 만 14세 미만 아동의 개인정보를 알면서 수집하지 않습니다. "
        "보호자가 아동의 글 삭제를 원하면 9번 창구로 알려 주세요.</p>"

        "<h2>11. 방침의 변경</h2>"
        f"<p>이 방침을 바꾸면 이 페이지에 시행일과 함께 알립니다. 현재 방침은 {EFFECTIVE}부터 적용됩니다.</p>"
    )


@router.api_route(PATH, methods=["GET", "HEAD"], response_class=HTMLResponse)
def privacy_page():
    from app.sportiq_seo import _page, _response
    ld = {"@context": "https://schema.org", "@type": "WebPage", "name": "SportIQ 개인정보처리방침",
          "url": "https://billnojay.com" + PATH, "inLanguage": "ko-KR"}
    return _response(_page(PATH, "개인정보처리방침 | SportIQ", "SportIQ(billnojay.com)가 처리하는 개인정보 항목, 목적, 보관 기간, 국외 이전, 이용자 권리와 문의 방법입니다.",
                           "개인정보처리방침", body_html(), ld))
