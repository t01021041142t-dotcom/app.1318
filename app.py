"""장보기 알림메모 — 실행: streamlit run app.py
GitHub에 app.py와 requirements.txt를 올리고 Streamlit Community Cloud에서 배포하세요.
배포 후 ⋯ 버튼에서 앱 공개 URL을 입력하면 같은 장보기를 여는 QR을 생성합니다.
주의: SQLite는 실행 서버의 로컬 저장소입니다. Cloud 재시작/재배포 시 유실될 수
있으므로 영구 보관 서비스가 아닙니다. 링크 보유자는 같은 목록을 조회/수정합니다.
"""
from __future__ import annotations

import csv
import io
import os
import sqlite3
import uuid
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urlsplit, urlunsplit, urlencode, parse_qsl

import qrcode
import streamlit as st

st.set_page_config(page_title="장보기 알림메모", page_icon="🛒", layout="centered", initial_sidebar_state="collapsed")
DB_PATH = os.environ.get("SHOPPING_DB_PATH", "shopping.sqlite3")
CATEGORIES = ["🥬 야채", "🍎 과일", "🥩 육류", "🐟 수산물", "🥛 유제품·달걀", "🍚 곡류·식재료", "🥫 가공식품", "🧴 생활용품", "🧺 기타"]
PRIORITY = {1: "1 · 필수", 2: "2 · 필요", 3: "3 · 여유"}


def connection():
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.row_factory = sqlite3.Row
    return con


def initialize():
    with connection() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS items (
            id TEXT PRIMARY KEY, list_id TEXT NOT NULL, category TEXT NOT NULL,
            name TEXT NOT NULL, priority INTEGER NOT NULL CHECK(priority BETWEEN 1 AND 3),
            planned_qty REAL NOT NULL CHECK(planned_qty > 0), unit TEXT NOT NULL,
            ceiling INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','deferred','bought')),
            price INTEGER NOT NULL DEFAULT 0 CHECK(price >= 0),
            quantity REAL NOT NULL DEFAULT 0 CHECK(quantity >= 0),
            total INTEGER NOT NULL DEFAULT 0 CHECK(total >= 0),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_list ON items(list_id)")


def records(list_id):
    with connection() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM items WHERE list_id=? ORDER BY priority, created_at, rowid", (list_id,))]


def amount(price, quantity):
    return int((Decimal(str(price)) * Decimal(str(quantity))).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def mutate(sql, params):
    with connection() as con:
        cur = con.execute(sql, params)
        return cur.rowcount


def safe_url(base, list_id):
    parts = urlsplit(base.strip())
    if parts.scheme not in ('https', 'http') or not parts.netloc or parts.username or parts.password:
        raise ValueError('https:// 로 시작하는 배포 앱 주소를 입력하세요.')
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != 'list']
    query.append(('list', list_id))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ''))


initialize()
raw_id = st.query_params.get('list', '')
try:
    list_id = str(uuid.UUID(raw_id))
except (ValueError, TypeError, AttributeError):
    list_id = str(uuid.uuid4())
    st.query_params['list'] = list_id

st.markdown('''<style>
.block-container {max-width:520px; padding:0.8rem 0.8rem 1rem;}
header[data-testid="stHeader"] {height:0; min-height:0;}
.stMainBlockContainer {padding-top:0.8rem;}
[data-testid="stVerticalBlock"] {gap:0.48rem;}
h1 {font-size:1.45rem!important; padding:0!important;}
p {margin-bottom:0.3rem;}
[data-testid="stMetricValue"] {font-size:1.6rem;}
[data-testid="stMetricLabel"] {font-size:0.8rem;}
.stButton button {min-height:2.65rem; border-radius:12px;}
[data-testid="stForm"] {padding:0.65rem;}
@media (max-width:600px) {
 .block-container {padding-top:0.5rem;}
 [data-testid="stHorizontalBlock"] {gap:0.4rem;}
 [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {min-width:0!important;}
}
</style>''', unsafe_allow_html=True)


@st.dialog('➕ 장볼 상품 추가')
def add_item():
    with st.form('add_form', clear_on_submit=False):
        category = st.selectbox('카테고리', CATEGORIES)
        name = st.text_input('상품명 *', max_chars=60, placeholder='예: 대파, 사과, 두부')
        a, b = st.columns(2)
        priority = a.selectbox('중요도', [1, 2, 3], format_func=lambda x: PRIORITY[x], index=1)
        qty = b.number_input('예정 수량', min_value=0.01, max_value=99999.0, value=1.0, step=1.0)
        a, b = st.columns(2)
        unit = a.selectbox('단위', ['개', '봉', '팩', 'kg', 'g', 'L', '묶음', '상자'])
        ceiling = b.number_input('단가 상한(원, 0=없음)', min_value=0, max_value=100000000, step=100)
        note = st.text_input('메모', max_chars=120, placeholder='예: 비싸면 다음에 / 국거리용')
        submitted = st.form_submit_button('메모 추가', use_container_width=True, type='primary')
    if submitted:
        if not name.strip():
            st.error('상품명을 입력해 주세요.')
            return
        mutate('''INSERT INTO items (id,list_id,category,name,priority,planned_qty,unit,ceiling,note)
                  VALUES (?,?,?,?,?,?,?,?,?)''',
               (str(uuid.uuid4()), list_id, category, name.strip(), priority, qty, unit, ceiling, note.strip()))
        st.rerun()


@st.dialog('🛒 카트에 담기')
def purchase(item):
    st.write(f"**{item['name']}** · {PRIORITY[item['priority']]}")
    if item['ceiling']:
        st.caption(f"설정한 단가 상한: {item['ceiling']:,}원 / {item['unit']}")
    st.caption('가격은 한 단위 기준입니다. 묶음 전체 가격이라면 수량을 1로 입력하세요.')
    price = st.number_input(f"실제 단가(원/{item['unit']})", min_value=0, max_value=100000000,
                            value=int(item['price']), step=100, key=f"price_{item['id']}")
    quantity = st.number_input(f"실제 수량({item['unit']})", min_value=0.01, max_value=99999.0,
                               value=float(item['planned_qty']), step=1.0, key=f"qty_{item['id']}")
    total = amount(price, quantity)
    st.info(f'이번 상품 합계: {total:,}원')
    expensive = bool(item['ceiling'] and price > item['ceiling'])
    if expensive:
        st.warning('설정한 단가 상한보다 비싸요. 지금 사지 않고 보류해도 괜찮아요.')
        approved = st.checkbox('상한 초과를 확인했고 이번에는 구매할게요', key=f"approve_{item['id']}")
    else:
        approved = True
    a, b = st.columns(2)
    if a.button('구매 확정', type='primary', use_container_width=True, disabled=not approved):
        changed = mutate("""UPDATE items SET status='bought',price=?,quantity=?,total=?
                            WHERE id=? AND list_id=? AND status IN ('pending','deferred')""",
                         (price, quantity, total, item['id'], list_id))
        if not changed:
            st.warning('다른 화면에서 변경된 상품입니다. 메인 화면을 새로고침해 주세요.')
        else:
            st.rerun()
    if b.button('비싸서 보류', use_container_width=True):
        mutate("UPDATE items SET status='deferred' WHERE id=? AND list_id=? AND status!='bought'", (item['id'], list_id))
        st.rerun()


@st.dialog('카트 상품 관리')
def cart_item(item):
    st.write(f"**{item['name']}**")
    st.write(f"{item['price']:,}원 × {item['quantity']:g}{item['unit']} = **{item['total']:,}원**")
    st.caption('잘못 담았거나 가격·수량을 바꾸려면 메모로 되돌린 후 다시 구매 확정하세요.')
    if st.button('↩ 메모로 되돌리기', use_container_width=True):
        mutate("UPDATE items SET status='pending',price=0,quantity=0,total=0 WHERE id=? AND list_id=?", (item['id'], list_id))
        st.rerun()


@st.dialog('메모 삭제')
def delete_item(item):
    st.write(f"‘{item['name']}’ 메모를 삭제할까요?")
    st.caption('구매가 아닙니다. 구매한 물건은 ‘카트 담기’에서 가격·수량을 기록하세요.')
    if st.button('메모만 삭제', type='primary', use_container_width=True):
        mutate("DELETE FROM items WHERE id=? AND list_id=? AND status!='bought'", (item['id'], list_id))
        st.rerun()


@st.dialog('QR · 공유 · 데이터')
def settings():
    st.caption('동일한 QR/링크를 가진 사람은 같은 장보기 목록을 보고 수정할 수 있어요. 공개 게시하지 마세요.')
    base = st.text_input('배포된 앱 URL', value=st.session_state.get('base_url', ''), placeholder='https://your-app.streamlit.app')
    if base:
        try:
            url = safe_url(base, list_id)
            st.session_state['base_url'] = base
            buf = io.BytesIO()
            qrcode.make(url).save(buf, format='PNG')
            st.image(buf.getvalue(), width=180)
            st.link_button('같은 장보기 열기', url, use_container_width=True)
            st.code(url, language=None, wrap_lines=True)
            st.download_button('QR 이미지 저장', buf.getvalue(), 'shopping-qr.png', 'image/png', use_container_width=True)
        except ValueError as exc:
            st.error(str(exc))
    st.warning('현재 데이터는 서버 임시 저장입니다. 재시작·재배포 시 사라질 수 있어요. 필요한 기록은 CSV로 보관하세요.')
    data = records(list_id)
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow(['카테고리','상품명','중요도','예정수량','단위','단가상한','메모','상태','실제단가','실제수량','합계'])
    for r in data:
        # 스프레드시트 수식 실행 방지
        def text(v):
            s = str(v)
            return "'" + s if s.lstrip().startswith(('=', '+', '-', '@')) else s
        writer.writerow([text(r['category']),text(r['name']),r['priority'],r['planned_qty'],text(r['unit']),
                         r['ceiling'],text(r['note']),{'pending':'대기','deferred':'보류','bought':'구매'}[r['status']],
                         r['price'],r['quantity'],r['total']])
    st.download_button('전체 내역 CSV 저장', output.getvalue().encode('utf-8-sig'), 'shopping.csv', 'text/csv', use_container_width=True)
    st.caption('기존 목록은 주소를 북마크해 보관하세요. 새 장보기는 별도 주소로 생성됩니다.')
    if st.button('빈 장보기 새로 만들기', use_container_width=True):
        st.query_params['list'] = str(uuid.uuid4())
        st.rerun()


items = records(list_id)
pending = [r for r in items if r['status'] == 'pending']
bought = [r for r in items if r['status'] == 'bought']
deferred = [r for r in items if r['status'] == 'deferred']

heading, menu = st.columns([8, 1])
heading.markdown('# 🛒 장보기 알림메모')
if menu.button('⋯', help='QR·공유·CSV 저장', use_container_width=True):
    settings()
a, b, c = st.columns(3)
a.metric('현재 카트 총액', f"{sum(r['total'] for r in bought):,}원")
b.metric('남은 메모', f'{len(pending)}개')
c.metric('보류 / 구매', f'{len(deferred)} / {len(bought)}')

view_col, add_col = st.columns([3, 1])
view = view_col.selectbox('보기', ['📝 살 것', '💭 보류', '🛒 카트'], label_visibility='collapsed')
if add_col.button('＋ 추가', use_container_width=True, type='primary'):
    add_item()

pool = pending if view == '📝 살 것' else deferred if view == '💭 보류' else bought
category = st.selectbox('카테고리 필터', ['전체 카테고리'] + CATEGORIES, label_visibility='collapsed')
filtered = [r for r in pool if category == '전체 카테고리' or r['category'] == category]
if filtered:
    by_id = {r['id']: r for r in filtered}
    def label(item_id):
        r = by_id[item_id]
        suffix = f" · {r['total']:,}원" if r['status'] == 'bought' else f" · {r['planned_qty']:g}{r['unit']}"
        return f"{PRIORITY[r['priority']]} | {r['name']}{suffix}"
    selected = st.selectbox('상품 선택 · 중요도 순', list(by_id), format_func=label, key=f'select_{view}_{category}')
    item = by_id[selected]
    with st.container(border=True):
        st.write(f"**{item['name']}**")
        st.caption(f"{item['category']} · {PRIORITY[item['priority']]} · 예정 {item['planned_qty']:g}{item['unit']}")
        if item['ceiling']:
            st.caption(f"단가 상한 {item['ceiling']:,}원/{item['unit']}")
        if item['note']:
            st.text(item['note'])
        if item['status'] == 'bought':
            st.write(f"{item['price']:,}원 × {item['quantity']:g}{item['unit']} = **{item['total']:,}원**")
            if st.button('구매 내역 관리', use_container_width=True):
                cart_item(item)
        else:
            a, b = st.columns([2, 1])
            if a.button('🛒 카트 담기', use_container_width=True, type='primary'):
                purchase(item)
            if item['status'] == 'pending':
                if b.button('보류', use_container_width=True):
                    mutate("UPDATE items SET status='deferred' WHERE id=? AND list_id=? AND status='pending'", (item['id'], list_id))
                    st.rerun()
            else:
                if b.button('다시 살 것', use_container_width=True):
                    mutate("UPDATE items SET status='pending' WHERE id=? AND list_id=? AND status='deferred'", (item['id'], list_id))
                    st.rerun()
            if st.button('메모 삭제', use_container_width=True):
                delete_item(item)
else:
    st.info('해당 상품이 없어요. ＋ 추가로 메모를 만들거나 다른 카테고리를 선택하세요.')
st.caption('1 필수 · 2 필요 · 3 여유 | 비싸면 보류해도 괜찮아요')
if st.button('↻ 새로고침', use_container_width=True, help='다른 기기의 변경 내용 반영'):
    st.rerun()
