"""가족 일정 알림판
실행: streamlit run app.py
GitHub에 app.py와 requirements.txt를 올리고 Streamlit Community Cloud에서 배포.
⋯ 메뉴에 배포 주소를 넣으면 같은 가족 게시판으로 연결되는 QR 생성.
SQLite 로컬 저장: 서버 초기화/재배포 시 유실 가능. 중요 기록은 JSON 백업.
링크 소지자는 모든 일정 조회/수정 가능. 의료정보 등 민감한 내용 입력 금지.
앱 내 알림만 제공하며 앱 종료 중 푸시/문자 알림은 제공하지 않음.
"""
from __future__ import annotations
import csv
import io
import json
import os
import sqlite3
import uuid
from datetime import datetime, date, time, timedelta
from urllib.parse import urlsplit, urlunsplit, urlencode, parse_qsl
from zoneinfo import ZoneInfo
import qrcode
import streamlit as st

st.set_page_config(page_title='우리집 일정', page_icon='📅', layout='centered', initial_sidebar_state='collapsed')
DB_PATH = os.environ.get('FAMILY_DB_PATH', 'family_board.sqlite3')
TZ = ZoneInfo('Asia/Seoul')
CATEGORIES = ['🏥 병원', '👨‍👩‍👧 가족 모임', '🏡 방문', '🧳 외출·여행', '🎂 기념일', '📌 기타']
IMPORTANCE = {1: '꼭 챙겨요', 2: '기억해요', 3: '참고해요'}


def connect():
    db = sqlite3.connect(DB_PATH, timeout=20)
    db.row_factory = sqlite3.Row
    return db


def initialize():
    with connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS events (
        id TEXT PRIMARY KEY, board TEXT NOT NULL, title TEXT NOT NULL,
        category TEXT NOT NULL, starts TEXT NOT NULL, place TEXT NOT NULL DEFAULT '',
        people TEXT NOT NULL DEFAULT '', prep TEXT NOT NULL DEFAULT '',
        importance INTEGER NOT NULL CHECK(importance BETWEEN 1 AND 3),
        done INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
        db.execute('''CREATE TABLE IF NOT EXISTS acknowledgements (
        event_id TEXT NOT NULL, board TEXT NOT NULL, person TEXT NOT NULL,
        confirmed TEXT NOT NULL, PRIMARY KEY(event_id,board,person))''')
        db.execute('CREATE INDEX IF NOT EXISTS board_events ON events(board,starts)')


def change(sql, args):
    with connect() as db:
        return db.execute(sql, args).rowcount


def events(board):
    with connect() as db:
        return [dict(r) for r in db.execute('SELECT * FROM events WHERE board=? ORDER BY starts,importance,created', (board,))]


def confirmations(eid, board):
    with connect() as db:
        return [r['person'] for r in db.execute('SELECT person FROM acknowledgements WHERE event_id=? AND board=? ORDER BY confirmed', (eid, board))]


def remove(eid, board):
    with connect() as db:
        db.execute('DELETE FROM acknowledgements WHERE event_id=? AND board=?', (eid, board))
        db.execute('DELETE FROM events WHERE id=? AND board=?', (eid, board))


def share_url(base, board):
    p = urlsplit(base.strip())
    if p.scheme not in ('https','http') or not p.netloc or p.username or p.password:
        raise ValueError('https:// 로 시작하는 배포 앱 주소를 입력하세요.')
    query = [(k,v) for k,v in parse_qsl(p.query, keep_blank_values=True) if k != 'family']
    query.append(('family',board))
    return urlunsplit((p.scheme,p.netloc,p.path,urlencode(query),''))


def starts(e):
    return datetime.fromisoformat(e['starts']).replace(tzinfo=TZ)


def date_label(e):
    dt = starts(e)
    return dt.strftime('%m/%d') + ' (' + '월화수목금토일'[dt.weekday()] + ') ' + dt.strftime('%H:%M')


initialize()
try:
    board = str(uuid.UUID(st.query_params.get('family','')))
except (ValueError,TypeError,AttributeError):
    board = str(uuid.uuid4())
    st.query_params['family'] = board

st.markdown('''<style>
.block-container {max-width:540px; padding:0.65rem 0.8rem 1rem;}
header[data-testid="stHeader"] {height:0; min-height:0;}
[data-testid="stVerticalBlock"] {gap:0.48rem;}
h1 {font-size:1.6rem!important;padding:0!important;}
h3 {font-size:1.35rem!important;}
p {font-size:1.05rem;margin-bottom:0.3rem;}
.stButton button {min-height:48px;border-radius:12px;font-size:1.05rem;}
[data-testid="stMetricValue"] {font-size:1.35rem;}
@media(max-width:600px){
[data-testid="stHorizontalBlock"] {gap:0.4rem;}
[data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {min-width:0!important;}
}
</style>''', unsafe_allow_html=True)


@st.dialog('📅 일정 등록·수정')
def editor(item=None):
    now = datetime.now(TZ)
    old_dt = starts(item) if item else now + timedelta(hours=1)
    with st.form('event_editor'):
        title = st.text_input('일정 이름 *', value=item['title'] if item else '', max_chars=60, placeholder='예: 치과 진료')
        category = st.selectbox('종류', CATEGORIES, index=CATEGORIES.index(item['category']) if item else 0)
        a,b = st.columns(2)
        day = a.date_input('날짜', value=old_dt.date(), min_value=date(2000,1,1), max_value=date(2100,12,31))
        clock = b.time_input('시간', value=old_dt.time().replace(second=0,microsecond=0,tzinfo=None), step=300)
        place = st.text_input('장소', value=item['place'] if item else '', max_chars=100)
        people = st.text_input('참석하는 가족', value=item['people'] if item else '', max_chars=100, placeholder='예: 엄마, 아빠, 민수')
        prep = st.text_area('준비물·메모', value=item['prep'] if item else '', max_chars=400, height=90, placeholder='예: 신분증, 진료카드 챙기기')
        priority = st.selectbox('중요도', [1,2,3], index=(item['importance']-1) if item else 1, format_func=lambda n: f'{n} · {IMPORTANCE[n]}')
        submitted = st.form_submit_button('저장하기', type='primary', use_container_width=True)
    if submitted:
        if not title.strip():
            st.error('일정 이름을 입력해 주세요.')
            return
        values = (title.strip(),category,datetime.combine(day,clock).isoformat(timespec='minutes'),place.strip(),people.strip(),prep.strip(),priority)
        if item:
            changed = change('UPDATE events SET title=?,category=?,starts=?,place=?,people=?,prep=?,importance=? WHERE id=? AND board=?', values+(item['id'],board))
            if not changed:
                st.error('이미 삭제된 일정입니다. 메인 화면을 새로고침해 주세요.')
                return
            # 변경된 일정은 가족이 다시 확인하도록 확인 기록 초기화
            change('DELETE FROM acknowledgements WHERE event_id=? AND board=?',(item['id'],board))
        else:
            change('INSERT INTO events(title,category,starts,place,people,prep,importance,id,board) VALUES(?,?,?,?,?,?,?,?,?)',values+(str(uuid.uuid4()),board))
        st.rerun()
    if item:
        st.caption('일정을 수정하면 가족 확인 기록이 초기화됩니다.')
        agreed = st.checkbox('이 일정을 완전히 삭제할게요')
        if st.button('일정 삭제', disabled=not agreed, use_container_width=True):
            remove(item['id'],board)
            st.rerun()


@st.dialog('✅ 일정 확인')
def confirm_event(item):
    st.write(f"**{item['title']}**")
    st.caption('가족 각자가 같은 이름을 사용하면 확인 여부를 구분할 수 있어요. 이름은 이 기기에서만 기억합니다.')
    name = st.text_input('내 이름 또는 호칭', value=st.session_state.get('person',''), max_chars=20, placeholder='예: 엄마')
    if st.button('확인했어요', type='primary', use_container_width=True):
        if not name.strip():
            st.error('이름을 입력해 주세요.')
            return
        with connect() as db:
            exists = db.execute('SELECT id FROM events WHERE id=? AND board=?',(item['id'],board)).fetchone()
            if not exists:
                st.error('이미 삭제된 일정입니다.')
                return
            db.execute('INSERT OR REPLACE INTO acknowledgements VALUES(?,?,?,?)',(item['id'],board,name.strip(),datetime.now(TZ).isoformat()))
        st.session_state['person'] = name.strip()
        st.rerun()


def backup(board):
    with connect() as db:
        acks = [dict(r) for r in db.execute('SELECT * FROM acknowledgements WHERE board=?',(board,))]
    return json.dumps({'version':1,'events':events(board),'acknowledgements':acks},ensure_ascii=False,indent=2).encode('utf-8')


def restore(raw, board):
    data = json.loads(raw)
    if data.get('version') != 1 or not isinstance(data.get('events'),list):
        raise ValueError('지원하지 않는 백업 형식입니다.')
    if len(data['events']) > 5000:
        raise ValueError('한 번에 최대 5,000개의 일정만 복원할 수 있어요.')
    prepared = []
    mapping = {}
    for r in data['events']:
        title = str(r['title']).strip()
        dt = datetime.fromisoformat(r['starts'])
        priority = int(r['importance'])
        if not title or len(title)>60 or not 2000<=dt.year<=2100 or priority not in IMPORTANCE or r['category'] not in CATEGORIES:
            raise ValueError('백업에 올바르지 않은 일정이 있어요.')
        new_id = str(uuid.uuid4())
        mapping[r['id']] = new_id
        prepared.append((new_id,board,title,r['category'],dt.replace(tzinfo=None).isoformat(timespec='minutes'),str(r.get('place',''))[:100],str(r.get('people',''))[:100],str(r.get('prep',''))[:400],priority,int(bool(r.get('done',0)))))
    with connect() as db:
        db.executemany('INSERT INTO events(id,board,title,category,starts,place,people,prep,importance,done) VALUES(?,?,?,?,?,?,?,?,?,?)', prepared)
        for a in data.get('acknowledgements',[]):
            if a.get('event_id') in mapping and str(a.get('person','')).strip():
                db.execute('INSERT OR REPLACE INTO acknowledgements VALUES(?,?,?,?)',(mapping[a['event_id']],board,str(a['person'])[:20],datetime.now(TZ).isoformat()))
    return len(prepared)


@st.dialog('QR · 공유 · 백업')
def settings():
    st.warning('가족 링크를 가진 사람은 모든 일정을 보고 수정할 수 있어요. 링크를 공개하거나 민감한 정보를 입력하지 마세요.')
    base = st.text_input('배포된 앱 주소', value=st.session_state.get('base_url',''), placeholder='https://your-app.streamlit.app')
    if base:
        try:
            url = share_url(base,board)
            st.session_state['base_url'] = base
            buf = io.BytesIO()
            qrcode.make(url).save(buf,format='PNG')
            st.image(buf.getvalue(),width=180)
            st.link_button('우리 가족 알림판 열기',url,use_container_width=True)
            st.code(url,language=None,wrap_lines=True)
            st.download_button('QR 이미지 저장',buf.getvalue(),'family-qr.png','image/png',use_container_width=True)
        except ValueError as exc:
            st.error(str(exc))
    st.info('처음 공유할 때 이 QR을 가족에게 보내세요. 각자 접속 후 브라우저의 ‘홈 화면에 추가’를 사용하면 편해요.')
    st.warning('서버 로컬 저장 방식입니다. 서버 초기화·재배포 시 데이터가 사라질 수 있으니 정기적으로 백업하세요.')
    st.download_button('일정 전체 백업(JSON)',backup(board),'family-backup.json','application/json',use_container_width=True)
    upload = st.file_uploader('백업 복원 · 기존 일정에 추가됩니다',type=['json'])
    agree = st.checkbox('복원은 추가 방식이며 반복하면 일정이 중복될 수 있음을 확인했어요')
    if st.button('백업 복원',disabled=not(upload and agree),use_container_width=True):
        try:
            if upload.size > 5_000_000:
                raise ValueError('백업은 5MB 이하만 지원합니다.')
            count = restore(upload.getvalue(),board)
            st.session_state['notice'] = f'{count}개 일정을 복원했어요.'
            st.rerun()
        except (ValueError,KeyError,TypeError,AttributeError) as exc:
            st.error(f'복원하지 못했어요. 백업 형식을 확인하세요. ({exc})')
    st.caption('이 앱은 한국 시간 기준입니다. 앱 종료 중 문자·푸시 알림은 전송하지 않습니다. 가족의 변경 내용은 새로고침으로 확인하세요.')


now = datetime.now(TZ)
rows = events(board)
active = [e for e in rows if not e['done']]
today = [e for e in active if starts(e).date() == now.date()]
late = [e for e in active if starts(e) < now]
head,menu = st.columns([8,1])
head.markdown('# 📅 우리집 일정')
if menu.button('⋯',help='QR 공유·백업',use_container_width=True):
    settings()
st.caption(f"{now.month}월 {now.day}일 {'월화수목금토일'[now.weekday()]}요일 · 가족이 함께 보는 알림판")
if 'notice' in st.session_state:
    st.success(st.session_state.pop('notice'))
a,b,c = st.columns(3)
a.metric('오늘 일정',f'{len(today)}개')
b.metric('남은 일정',f'{len(active)}개')
c.metric('시간 지난 미완료',f'{len(late)}개')
left,right = st.columns([3,1])
view = left.selectbox('일정 보기',['다가오는 일정','오늘 일정','시간 지난 일정','완료한 일정'],label_visibility='collapsed')
if right.button('＋ 등록',type='primary',use_container_width=True):
    editor()
if view == '다가오는 일정':
    pool = [e for e in active if starts(e)>=now]
elif view == '오늘 일정':
    pool = today
elif view == '시간 지난 일정':
    pool = late
else:
    pool = sorted([e for e in rows if e['done']],key=lambda e:e['starts'],reverse=True)

if pool:
    ids = [e['id'] for e in pool]
    lookup = {e['id']:e for e in pool}
    eid = st.selectbox('일정 선택',ids,format_func=lambda i:f"{date_label(lookup[i])} · {lookup[i]['title']}",key='event_'+view)
    item = lookup[eid]
    with st.container(border=True):
        st.subheader(item['title'])
        st.write('🕒 **'+date_label(item)+'**')
        st.caption(f"{item['category']} · {IMPORTANCE[item['importance']]}")
        # 고정 높이 영역으로 긴 메모가 주 화면을 밀어내지 않도록 제한
        with st.container(height=115,border=False):
            st.write('📍 '+(item['place'] or '장소 미입력'))
            if item['people']:
                st.write('👪 '+item['people'])
            st.write('🎒 '+(item['prep'] or '준비물 없음'))
            names = confirmations(item['id'],board)
            st.caption('확인: '+(', '.join(names) if names else '아직 없어요'))
        a,b = st.columns(2)
        if item['done']:
            if a.button('↩ 완료 취소',use_container_width=True):
                change('UPDATE events SET done=0 WHERE id=? AND board=?',(eid,board))
                st.rerun()
        else:
            if a.button('✅ 확인했어요',type='primary',use_container_width=True):
                confirm_event(item)
            if b.button('🏁 일정 완료',use_container_width=True):
                change('UPDATE events SET done=1 WHERE id=? AND board=?',(eid,board))
                st.rerun()
        if st.button('일정 수정·삭제',use_container_width=True):
            editor(item)
else:
    st.info('이 보기에 일정이 없어요. ＋ 등록을 눌러 가족 일정을 추가하세요.')
if late and view != '시간 지난 일정':
    st.caption(f'🔔 시간이 지난 미완료 일정 {len(late)}개는 ‘시간 지난 일정’에서 확인하세요.')
if st.button('↻ 가족 변경 내용 새로고침',use_container_width=True):
    st.rerun()
