"""MODO / 실행: streamlit run app.py
GitHub에 app.py와 requirements.txt를 같은 위치에 업로드합니다.
SQLite는 로컬 시연용입니다. Cloud 초기화 시 데이터가 유실될 수 있습니다.
가족 비밀번호 소지자는 모든 구성원 기록을 조회·삭제할 수 있습니다.
자동 푸시/응급 감시/진단 기능 없음. 임상 NPRS 대신 3단계 주관적 불편도 사용.
"""
from __future__ import annotations
import time
import hashlib
import hmac
import io
import json
import os
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from urllib.parse import urlsplit, urlunsplit, urlencode, parse_qsl
from zoneinfo import ZoneInfo
import plotly.graph_objects as pg
import qrcode
import streamlit as st

DB = os.environ.get('CARE_DB_PATH', 'family_care.sqlite3')
TZ = ZoneInfo('Asia/Seoul')
LEVELS = {0: '편안해요', 1: '살짝 불편', 2: '중간 정도', 3: '많이 불편'}
REGIONS = {
    '머리·얼굴': ['머리', '눈 주변', '귀', '코 주변', '턱·입 주변'],
    '목·어깨': ['목 앞쪽', '목 뒤쪽', '왼쪽 어깨', '오른쪽 어깨'],
    '몸통': ['가슴', '명치', '윗배', '아랫배', '왼쪽 옆구리', '오른쪽 옆구리', '등', '허리'],
    '팔·손': ['왼쪽 위팔', '오른쪽 위팔', '왼쪽 팔꿈치', '오른쪽 팔꿈치', '왼쪽 아래팔', '오른쪽 아래팔', '왼쪽 손목', '오른쪽 손목', '왼쪽 손·손가락', '오른쪽 손·손가락'],
    '골반·엉덩이': ['골반 가운데', '왼쪽 골반·고관절 주변', '오른쪽 골반·고관절 주변', '왼쪽 사타구니', '오른쪽 사타구니', '왼쪽 엉덩이', '오른쪽 엉덩이', '꼬리뼈 주변'],
    '다리·발': ['왼쪽 허벅지', '오른쪽 허벅지', '왼쪽 무릎', '오른쪽 무릎', '왼쪽 정강이', '오른쪽 정강이', '왼쪽 종아리', '오른쪽 종아리', '왼쪽 발목', '오른쪽 발목', '왼쪽 발등', '오른쪽 발등', '왼쪽 발바닥·뒤꿈치', '오른쪽 발바닥·뒤꿈치', '왼쪽 발가락', '오른쪽 발가락'],
    '넓게 불편해요': ['전신', '여러 관절', '여러 근육'],
}
PARTS = sorted({part for items in REGIONS.values() for part in items})

def now():
    return datetime.now(TZ)

@contextmanager
def db():
    con = sqlite3.connect(DB, timeout=20)
    con.row_factory = sqlite3.Row
    try:
        con.execute('PRAGMA foreign_keys=ON')
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

def initialize():
    with db() as con:
        # 기존 앱의 테이블 구조와 호환됩니다.
        con.execute('CREATE TABLE IF NOT EXISTS families(id TEXT PRIMARY KEY,salt TEXT NOT NULL,digest TEXT NOT NULL)')
        con.execute('CREATE TABLE IF NOT EXISTS entries(id TEXT PRIMARY KEY,family TEXT NOT NULL REFERENCES families(id),person TEXT NOT NULL,part TEXT NOT NULL,level INTEGER NOT NULL CHECK(level BETWEEN 0 AND 3),stamp TEXT NOT NULL,seen INTEGER NOT NULL DEFAULT 0)')
        con.execute('CREATE TABLE IF NOT EXISTS members(family TEXT NOT NULL REFERENCES families(id),name TEXT NOT NULL,PRIMARY KEY(family,name))')
        con.execute('CREATE INDEX IF NOT EXISTS idx_family_time ON entries(family,stamp)')

def hashed(password, salt):
    return hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),260000).hex()

def create_family(password):
    if len(password)<10:
        raise ValueError('비밀번호는 10자 이상으로 설정해 주세요.')
    fid, salt = uuid.uuid4().hex, secrets.token_hex(16)
    with db() as con:
        con.execute('INSERT INTO families VALUES(?,?,?)',(fid,salt,hashed(password,salt)))
    return fid

def login(fid,password):
    with db() as con:
        row=con.execute('SELECT * FROM families WHERE id=?',(fid,)).fetchone()
    return bool(row and hmac.compare_digest(row['digest'],hashed(password,row['salt'])))

def add_member(fid,name):
    name=name.strip()
    if not name or len(name)>16:
        raise ValueError('이름·별명은 1~16자로 입력해 주세요.')
    with db() as con:
        con.execute('INSERT OR IGNORE INTO members VALUES(?,?)',(fid,name))

def members(fid):
    with db() as con:
        rows=con.execute('SELECT name FROM members WHERE family=? UNION SELECT person AS name FROM entries WHERE family=? ORDER BY name',(fid,fid)).fetchall()
    return [r['name'] for r in rows]

def records(fid,person=None):
    with db() as con:
        if person is None:
            rows=con.execute('SELECT * FROM entries WHERE family=? ORDER BY stamp DESC,rowid DESC',(fid,)).fetchall()
        else:
            rows=con.execute('SELECT * FROM entries WHERE family=? AND person=? ORDER BY stamp DESC,rowid DESC',(fid,person)).fetchall()
    return [dict(r) for r in rows]

def save(fid,person,part,level,token):
    if person not in members(fid):
        raise ValueError('기록할 가족을 다시 선택해 주세요.')
    if not ((level==0 and part=='불편 없음') or (level in (1,2,3) and part in PARTS)):
        raise ValueError('부위와 불편도를 다시 선택해 주세요.')
    # 동일 저장 토큰은 한 번만 기록: 재실행/중복 클릭 방지.
    with db() as con:
        con.execute('INSERT OR IGNORE INTO entries VALUES(?,?,?,?,?,?,0)',(token,fid,person,part,level,now().isoformat()))

def read_entries(fid,ids):
    with db() as con:
        con.executemany('UPDATE entries SET seen=1 WHERE family=? AND id=?',[(fid,i) for i in ids])

def remove(fid,eid):
    with db() as con:
        con.execute('DELETE FROM entries WHERE family=? AND id=?',(fid,eid))

def day(r):
    return datetime.fromisoformat(r['stamp']).astimezone(TZ).date()

def signals(rows):
    today=now().date()
    out=[]
    for person,part in sorted({(r['person'],r['part']) for r in rows if r['level']>0}):
        rs=[r for r in rows if r['person']==person and r['part']==part and r['level']>0]
        dates={day(r) for r in rs}
        cursor=today if today in dates else today-timedelta(days=1)
        count=0
        while cursor in dates:
            count+=1
            cursor-=timedelta(days=1)
        high=any(day(r)==today and r['level']==3 for r in rs)
        if high or count>=3:
            reason=[]
            if high: reason.append('오늘 많이 불편')
            if count>=3: reason.append(f'{count}일 연속 기록')
            out.append((person,part,' · '.join(reason)))
    return out

def family_url(base,fid):
    u=urlsplit(base.strip())
    if u.scheme!='https' or not u.netloc or u.username or u.password:
        raise ValueError('https://로 시작하는 앱 주소를 입력해 주세요.')
    q=[(k,v) for k,v in parse_qsl(u.query) if k!='family']
    return urlunsplit((u.scheme,u.netloc,u.path,urlencode(q+[('family',fid)]),''))

def go(page):
    st.session_state.page=page
    st.rerun()

def start_record(person):
    st.session_state.who=person
    st.session_state.token=uuid.uuid4().hex
    st.session_state.pop('part',None)
    go('body')

def complete(fid,part,level):
    save(fid,st.session_state.who,part,level,st.session_state.token)
    st.session_state.saved_part=part
    st.session_state.saved_level=level
    go('done')

def button(label,page,key=None):
    if st.button(label,key=key,use_container_width=True): go(page)

def back(page='home'):
    if st.button('‹ 돌아가기',key='back'): go(page)

# 인체 비율을 본뜬 연속 곡선 실루엣. 전면/후면에서 클릭점과 라벨을 분리합니다.
OUTLINE = "M -.25,8.62 C -.25,8.5 -.27,8.38 -.38,8.32 C -.65,8.2 -.94,8.18 -1.09,8.02 C -1.25,7.83 -1.3,7.51 -1.37,7.18 L -1.66,6.12 L -1.86,5.26 C -1.91,5.1 -2.05,4.95 -2.06,4.79 C -2.08,4.62 -1.98,4.59 -1.91,4.72 L -1.86,4.85 C -1.91,4.57 -1.82,4.5 -1.76,4.68 C -1.74,4.5 -1.66,4.55 -1.65,4.74 L -1.66,5.04 L -1.47,5.76 L -1.12,6.65 L -.98,7.18 C -.91,6.89 -.86,6.59 -.82,6.32 C -.78,6.0 -.91,5.65 -.94,5.38 C -1.06,5.02 -.99,4.56 -.9,4.22 L -.67,3.13 C -.62,2.89 -.59,2.77 -.6,2.56 C -.7,2.08 -.58,1.57 -.5,1.13 L -.46,.66 C -.47,.48 -.63,.34 -.65,.23 C -.66,.12 -.48,.1 -.23,.13 C -.04,.16 -.11,.35 -.15,.52 L -.18,.85 C -.14,1.39 -.12,1.92 -.23,2.47 L -.22,2.87 L -.09,4.15 Q 0,4.38 .09,4.15 L .22,2.87 L .23,2.47 C .12,1.92 .14,1.39 .18,.85 L .15,.52 C .11,.35 .04,.16 .23,.13 C .48,.1 .66,.12 .65,.23 C .63,.34 .47,.48 .46,.66 L .5,1.13 C .58,1.57 .7,2.08 .6,2.56 C .59,2.77 .62,2.89 .67,3.13 L .9,4.22 C .99,4.56 1.06,5.02 .94,5.38 C .91,5.65 .78,6 .82,6.32 C .86,6.59 .91,6.89 .98,7.18 L 1.12,6.65 L 1.47,5.76 L 1.66,5.04 L 1.65,4.74 C 1.66,4.55 1.74,4.5 1.76,4.68 C 1.82,4.5 1.91,4.57 1.86,4.85 L 1.91,4.72 C 1.98,4.59 2.08,4.62 2.06,4.79 C 2.05,4.95 1.91,5.1 1.86,5.26 L 1.66,6.12 L 1.37,7.18 C 1.3,7.51 1.25,7.83 1.09,8.02 C .94,8.18 .65,8.2 .38,8.32 C .27,8.38 .25,8.5 .25,8.62 C .45,8.75 .55,8.98 .56,9.21 C .71,9.23 .69,9.54 .57,9.52 C .59,9.92 .4,10.17 0,10.18 C -.4,10.17 -.59,9.92 -.57,9.52 C -.69,9.54 -.71,9.23 -.56,9.21 C -.55,8.98 -.45,8.75 -.25,8.62 Z"


def body_map():
    facing=st.segmented_control('신체 방향',['앞면','뒷면'],default='앞면',key='facing') or '앞면'
    # 큰 영역 먼저 선택하여 작은 화면에서 라벨이 겹치지 않도록 합니다.
    pts=[('머리·얼굴',0,9.5,2.9),('목·어깨',0,8.15,-2.9),
         ('몸통',0,6.65,2.9),('팔·손',-1.7,5.4,-2.9),
         ('골반·엉덩이',0,4.7,2.9),('다리·발',-.55,2.3,-2.9)]
    fig=go_plot(pts,facing)
    generation=st.session_state.get('map_gen',0)
    event=st.plotly_chart(fig,key=f'map_{generation}_{facing}',on_select='rerun',selection_mode='points',use_container_width=True,config={'displayModeBar':False,'scrollZoom':False})
    if event.selection.points:
        region=event.selection.points[-1].get('customdata')
        if isinstance(region,str) and region in REGIONS:
            st.session_state.region=region
            st.session_state.map_gen=generation+1
            go('region')
    button('부위 이름으로 선택', 'parts')


def region_screen():
    back('body')
    region=st.session_state.get('region')
    if region not in REGIONS:
        go('body')
    st.caption(f"{st.session_state.who} · 2 / 3")
    st.subheader(region)
    st.caption('본인 기준 왼쪽·오른쪽')
    cols=st.columns(2)
    for i,part in enumerate(REGIONS[region]):
        if cols[i%2].button(part,key='detail_'+part,use_container_width=True):
            st.session_state.part=part
            go('level')


def go_plot(pts,facing="앞면"):
    fig=pg.Figure()
    fig.add_shape(type='path',path=OUTLINE,fillcolor='#E3E8ED',line={'color':'#96A5B4','width':1.3},layer='below')
    # 신체 구조를 드러내는 쇄골·관절·몸통 윤곽선.
    if facing=='앞면':
        details=[
            'M -.79,8.02 Q -.42,7.92 -.1,7.98 M .79,8.02 Q .42,7.92 .1,7.98',
            'M 0,7.82 L 0,6.92',
            'M -.72,5.48 Q -.38,5.14 0,4.55 Q .38,5.14 .72,5.48',
            'M -.54,2.9 Q -.4,2.79 -.25,2.91 M .54,2.9 Q .4,2.79 .25,2.91',
            'M -.49,9.15 Q -.33,8.8 0,8.73 Q .33,8.8 .49,9.15',
        ]
    else:
        details=[
            'M 0,8.14 Q -.07,7.04 0,5.65',
            'M -.72,7.81 Q -.91,7.24 -.43,6.94 M .72,7.81 Q .91,7.24 .43,6.94',
            'M -.82,4.92 Q -.45,4.57 0,4.85 Q .45,4.57 .82,4.92',
            'M 0,4.85 L 0,4.4',
            'M -.54,2.8 L -.28,2.8 M .54,2.8 L .28,2.8',
        ]
    for path in details:
        fig.add_shape(type='path',path=path,line={'color':'#B7C2CC','width':1.1},layer='below')
    if facing=='앞면':
        fig.add_shape(type='circle',x0=-.035,x1=.035,y0=6.1,y1=6.17,fillcolor='#B1BCC7',line_width=0,layer='below')
    for part,x,y,lx in pts:
        fig.add_shape(type='line',x0=x,x1=lx*.82,y0=y,y1=y,line={'color':'#C4D3DC','width':1},layer='below')
    # 신체 부위와 글자 라벨 양쪽 모두 같은 선택 이벤트를 반환합니다.
    fig.add_trace(pg.Scatter(x=[p[1] for p in pts],y=[p[2] for p in pts],mode='markers',customdata=[p[0] for p in pts],marker={'size':17,'color':'#3182F6','opacity':.8,'line':{'color':'white','width':2}},hovertemplate='%{customdata}<extra></extra>'))
    fig.add_trace(pg.Scatter(x=[p[3] for p in pts],y=[p[2] for p in pts],mode='markers+text',text=[p[0].replace('왼쪽','왼').replace('오른쪽','오른') for p in pts],customdata=[p[0] for p in pts],textfont={'size':12,'color':'#18384B'},marker={'size':44,'color':'#FFFFFF','opacity':.95},hovertemplate='%{customdata}<extra></extra>'))
    fig.update_layout(height=300,showlegend=False,clickmode='event+select',dragmode=False,margin={'l':0,'r':0,'t':2,'b':0},paper_bgcolor='white',plot_bgcolor='white',xaxis={'visible':False,'range':[-4.05,4.05],'fixedrange':True},yaxis={'visible':False,'range':[0,10.5],'fixedrange':True,'scaleanchor':'x','scaleratio':1})
    return fig

def login_screen():
    st.subheader('가족의 몸 상태를, 함께 살펴요')
    st.caption('부위를 누르고 불편도를 고르면 가족에게 공유돼요.')
    a,b=st.columns(2)
    with a:
        with st.container(border=True):
            st.write('**손쉽게 기록**')
            st.caption('글을 쓰지 않고 부위로 선택')
    with b:
        with st.container(border=True):
            st.write('**가족과 공유**')
            st.caption('새 기록과 반복된 불편 확인')
    mode=st.session_state.get('entry_mode')
    if mode is None:
        if st.button('처음이에요 · 가족 공간 만들기',type='primary',use_container_width=True):
            st.session_state.entry_mode='create';st.rerun()
        if st.button('초대받았어요 · 가족과 연결',use_container_width=True):
            st.session_state.entry_mode='join';st.rerun()
        st.caption('진단 앱이 아닌 가족 건강 기록장입니다.')
        return
    if st.button('‹ 시작 화면'):
        st.session_state.pop('entry_mode',None);st.rerun()
    if mode=='create':
        st.write('**가족이 함께 쓸 비밀번호를 정해 주세요**')
        with st.form('create_family'):
            password=st.text_input('가족 비밀번호',type='password',help='10자 이상. 가족에게만 알려 주세요.')
            again=st.text_input('비밀번호 확인',type='password')
            submitted=st.form_submit_button('만들고 시작하기',use_container_width=True)
        if submitted:
            # 가입 입력 오류에는 대기시간이나 로그인 실패 횟수를 적용하지 않음.
            if len(password)<10:
                st.error('비밀번호를 10자 이상 입력해 주세요. 바로 수정할 수 있어요.')
            elif password!=again:
                st.error('두 비밀번호가 달라요. 확인 칸을 다시 입력해 주세요.')
            else:
                st.session_state.fid=create_family(password)
                go('setup')
        st.caption('가족 ID는 자동으로 만들어져요. 비밀번호 복구는 지원하지 않아요.')
    else:
        st.write('**가족에게 받은 정보로 연결해요**')
        with st.form('join_family'):
            fid=st.text_input('초대받은 가족 ID',value=st.query_params.get('family',''))
            password=st.text_input('가족 비밀번호',type='password')
            submitted=st.form_submit_button('우리 가족 연결하기',use_container_width=True)
        if submitted:
            if not fid.strip() or not password:
                st.error('가족 ID와 비밀번호를 입력해 주세요.')
            elif login(fid.strip(),password):
                st.session_state.fid=fid.strip()
                go('home' if members(fid.strip()) else 'setup')
            else:
                st.error('연결 정보를 확인해 주세요. 가족 ID는 이름이 아닌 초대 코드예요.')
        st.caption('가족 ID와 비밀번호는 먼저 공간을 만든 가족에게 받아 주세요.')


def setup_screen(fid):
    back('settings' if members(fid) else 'home')
    st.subheader('기록할 가족 이름을 알려 주세요')
    with st.form('add_member'):
        name=st.text_input('이름·별명',max_chars=16,placeholder='예: 엄마, 아빠')
        ok=st.form_submit_button('등록하고 시작하기',use_container_width=True)
    if ok:
        try: add_member(fid,name)
        except ValueError as e: st.error(str(e))
        else: go('home')

def home_screen(fid):
    names=members(fid)
    if not names:
        st.subheader('내 상태 남기기')
        st.caption('처음 한 번 기록할 이름을 등록해 주세요.')
        button('이름 등록하기','setup')
        return
    if st.session_state.get('who') not in names:
        st.session_state.who=names[0]
    if not st.session_state.get('token'):
        st.session_state.token=uuid.uuid4().hex
    a,b=st.columns([3,1])
    with a: st.subheader('내 상태 남기기')
    with b:
        if st.button('👤',key='switch_person',help='기록할 가족 바꾸기'): go('choose')
    st.caption(st.session_state.who+' · 불편한 부위를 선택하세요')
    body_map()
    if st.button('😊 오늘은 편안해요',use_container_width=True):
        complete(fid,'불편 없음',0)


def choose_screen(fid):
    back()
    st.subheader('누구의 상태를 기록할까요?')
    names=members(fid)
    if not names: button('가족 추가','setup'); return
    cols=st.columns(2)
    for i,name in enumerate(names):
        if cols[i%2].button('👤 '+name,key='who_'+name,use_container_width=True): start_record(name)

def body_screen(fid):
    back('choose')
    st.caption(f'{st.session_state.who}  ·  1 / 3')
    st.subheader('어디가 불편하세요?')
    body_map()
    if st.button('😊 오늘은 편안해요',use_container_width=True): complete(fid,'불편 없음',0)

def parts_screen():
    back('body')
    st.subheader('部位を選択')
    cols=st.columns(2)
    for i,region in enumerate(REGIONS):
        if cols[i%2].button(region,key='region_'+region,use_container_width=True):
            st.session_state.region=region
            go('region')


def level_screen(fid):
    back('region')
    st.caption(f'{st.session_state.who}  ·  3 / 3')
    st.subheader(st.session_state.part)
    st.write('얼마나 불편하세요?')
    for level,emoji in [(1,'🙂'),(2,'😟'),(3,'😣')]:
        if st.button(f'{emoji}  {LEVELS[level]}  ·  저장',key=f'level{level}',type='primary',use_container_width=True): complete(fid,st.session_state.part,level)

def done_screen(fid):
    st.subheader('✅ 가족에게 공유했어요')
    st.write(f"{st.session_state.who} · {st.session_state.saved_part}")
    st.metric('오늘의 상태',LEVELS[st.session_state.saved_level])
    if st.session_state.saved_level==3:
        st.warning('새롭거나 심한 불편은 의료진과 상담하세요. 응급 증상은 119.')
    button('기록 끝내기 · 홈','home')
    if st.button('다른 부위도 기록',use_container_width=True): start_record(st.session_state.who)
    if st.button('방금 기록 취소'):
        remove(fid,st.session_state.token)
        go('home')

def family_screen(fid):
    back()
    st.subheader('가족 상태 확인')
    if st.button('↻ 새로고침'): st.rerun()
    rows=records(fid)
    unread=[r for r in rows if not r['seen']]
    a,b=st.columns(2)
    a.metric('미확인',len(unread)); b.metric('확인 완료',len(rows)-len(unread))
    mode=st.segmented_control('확인할 내용',['새 기록','주의 신호'],default='새 기록') or '새 기록'
    if mode=='주의 신호':
        items=signals(rows)
        if not items: st.success('현재 기록 기준 주의 신호가 없어요.')
        for person,part,reason in items:
            with st.container(border=True):
                st.write(f'**{person} · {part}**'); st.write(reason)
                st.caption('직접 연락해서 상태를 확인해 주세요.')
        return
    if not unread: st.success('새 기록을 모두 확인했어요.'); return
    # 한 번에 한 기록만 확인: 읽음 처리 후 카운트와 다음 기록 즉시 갱신.
    row=unread[0]
    with st.container(border=True):
        st.subheader(f"{row['person']} · {row['part']}")
        st.write(LEVELS[row['level']])
        st.caption(datetime.fromisoformat(row['stamp']).astimezone(TZ).strftime('%m/%d %H:%M'))
        if st.button('확인했어요 · 다음 기록',use_container_width=True):
            read_entries(fid,[row['id']]); st.rerun()

def history_screen(fid):
    back()
    st.subheader('기록 돌아보기')
    person=st.session_state.get('history_person')
    if not person:
        for name in members(fid):
            if st.button('👤 '+name,key='history_'+name,use_container_width=True):
                st.session_state.history_person=name; st.rerun()
        return
    if st.button('다른 가족 선택'):
        st.session_state.pop('history_person',None); st.session_state.history_index=0; st.rerun()
    rows=records(fid,person)
    if not rows: st.info('아직 기록이 없어요.'); return
    index=min(max(0,st.session_state.get('history_index',0)),len(rows)-1)
    row=rows[index]
    with st.container(border=True):
        st.caption(f'{person} · {index+1} / {len(rows)}')
        st.subheader(row['part']); st.write(LEVELS[row['level']])
        st.caption(datetime.fromisoformat(row['stamp']).astimezone(TZ).strftime('%Y/%m/%d %H:%M'))
    a,b=st.columns(2)
    if a.button('‹ 최신 쪽',disabled=index==0): st.session_state.history_index=index-1; st.rerun()
    if b.button('이전 기록 ›',disabled=index==len(rows)-1): st.session_state.history_index=index+1; st.rerun()
    if st.button('이 기록 삭제'): st.session_state.delete_id=row['id']; go('delete')

def settings_screen(fid):
    back()
    st.subheader('가족 연결')
    st.caption('가족 ID · 비밀번호는 별도로 전달하세요.')
    st.code(fid,language=None)
    button('＋ 가족 이름 추가','setup')
    button('QR로 연결하기','qr')
    backup=json.dumps({'format':1,'exported_at':now().isoformat(),'entries':records(fid)},ensure_ascii=False,indent=2)
    st.download_button('기록 백업 내려받기',backup.encode(),'care_backup.json','application/json',use_container_width=True)
    button('로그아웃','logout')

def qr_screen(fid):
    back('settings')
    st.subheader('가족 QR 만들기')
    with st.form('qr_form'):
        base=st.text_input('배포된 앱 주소',placeholder='https://your-app.streamlit.app')
        ok=st.form_submit_button('QR 만들기',use_container_width=True)
    if ok:
        try:
            link=family_url(base,fid); out=io.BytesIO(); qrcode.make(link).save(out,format='PNG')
            st.session_state.qr=out.getvalue(); st.session_state.link=link
        except ValueError as e: st.error(str(e))
    if st.session_state.get('qr'):
        st.image(st.session_state.qr,width=190)
        st.download_button('QR 저장',st.session_state.qr,'family_qr.png','image/png')
        st.code(st.session_state.link,language=None)

def help_screen():
    back(st.session_state.get('help_return','home'))
    st.subheader('이용 안내')
    st.write('**기록:** 가족 선택 → 신체 영역 → 세부 부위 → 불편도 → 저장 완료')
    st.write('**가족 확인:** 새 기록을 하나씩 읽고 확인 버튼을 누르세요. 자동 푸시·문자 전송은 없습니다.')
    st.write('**주의 신호:** 오늘 ‘많이’ 기록 또는 같은 부위 3일 이상 연속 기록을 표시합니다. 같은 날은 한 번으로 계산하며, 실제 통증 지속·회복을 판정하지 않습니다.')
    st.write('3단계는 주관적 불편도입니다. 임상 NPRS(0~10)·VAS가 아니며 진단·치료에 사용하지 않습니다. 찜질·약 복용을 자동 추천하지 않습니다.')
    st.warning('갑작스러운 심한 가슴 통증·숨참, 한쪽 마비·말 어눌함, 의식 변화 등은 119에 연락하세요. 가족의 앱 확인을 기다리지 마세요.')
    st.write('가족 비밀번호를 가진 사람은 모든 가족의 기록을 조회·삭제할 수 있습니다. 구성원 동의를 받고 최소한의 정보만 입력하세요. 개인별 권한 인증은 없습니다.')
    st.write('로컬 SQLite 시연용: 서버 초기화·재배포 시 유실될 수 있습니다. 백업에는 건강정보가 포함됩니다. 백업 복원·비밀번호 복구는 지원하지 않습니다. 실제 운영 전 영구 DB와 개인정보 보호 보강이 필요합니다.')
    st.caption('휴대폰 크기에 따라 스크롤이 필요합니다. 공개 운영 전 서버 측 로그인 보호와 개인별 권한 인증을 보강해야 합니다.')

def main():
    st.set_page_config(page_title='MODO',page_icon='🌿',layout='centered')
    st.markdown('''<style>
    :root {color-scheme:light}
    [data-testid="stAppViewContainer"]{background:#FAF8F4}
    .block-container{max-width:540px;padding-top:1rem;padding-bottom:1rem}
    header[data-testid="stHeader"]{position:relative;height:2rem;background:#FAF8F4}
    [data-testid="stVerticalBlock"]{gap:.55rem}
    .st-key-main_nav button{min-height:48px!important;border-radius:14px!important;padding:3px!important}
    .st-key-main_nav button p{font-size:.76rem!important}
    .modo-splash{text-align:center;font-size:clamp(3rem,18vw,5rem);font-weight:800;letter-spacing:.12em;color:#365347;padding-top:30vh;animation:modo-in .7s ease}
    @keyframes modo-in{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:translateY(0)}}
    h1{font-size:clamp(1.4rem,5vw,2rem)!important;font-weight:800!important;color:#191F28!important;letter-spacing:-.05em}
    h3{font-size:1.4rem!important;font-weight:750!important;color:#191F28!important;letter-spacing:-.035em}
    p,label{color:#333D4B}
    [data-testid="stCaptionContainer"] p{color:#6B7684;font-size:.88rem}
    div.stButton>button,div.stFormSubmitButton>button{
      min-height:60px;border-radius:20px;border:1px solid #E8EDF3;
      background:#FFFFFF;color:#333D4B;font-size:1.08rem;font-weight:650;
      box-shadow:0 3px 12px rgba(25,31,40,.035);transition:background .15s,border .15s}
    div.stButton>button:hover{background:#F1EEE8;border-color:#CDC5B9;color:#353C37}
    div.stButton>button:focus-visible{outline:3px solid #8AAB99;outline-offset:2px}
    div.stButton>button[kind="primary"],div.stFormSubmitButton>button{
      background:#DCEADF;color:#263D30;border-color:#CADCCF}
    div.stButton>button[kind="primary"]:hover,div.stFormSubmitButton>button:hover{background:#CADFCE;color:#263D30}
    div.stButton>button[kind="primary"] p,div.stFormSubmitButton>button p{color:#263D30!important}
    div.stButton>button p{font-weight:650}
    [data-testid="stMetric"]{background:white;border-radius:22px;padding:20px;border:1px solid #EDF0F4}
    [data-testid="stMetricValue"]{color:#191F28;font-weight:750}
    [data-testid="stTextInput"] input{background:white;border-radius:14px;min-height:48px;color:#191F28}
    [data-testid="stForm"]{background:white;border:1px solid #EDF0F4;border-radius:22px;padding:24px}
    [data-testid="stVerticalBlockBorderWrapper"]{border-radius:22px!important}
    [data-testid="stAlert"]{border-radius:18px}
    @media(max-width:480px){.block-container{padding-left:1rem;padding-right:1rem}div.stButton>button{font-size:1rem;min-height:58px}}
    </style>''',unsafe_allow_html=True)
    initialize()
    if not st.session_state.get('splash_done'):
        st.session_state.splash_done=True
        holder=st.empty()
        holder.markdown('<div class="modo-splash">MODO</div>',unsafe_allow_html=True)
        time.sleep(3)
        holder.empty()
        st.rerun()
    page=st.session_state.get('page','home')
    fid=st.session_state.get('fid')
    if not fid:
        if page=='help': help_screen()
        else: login_screen()
        return
    with st.container(key='main_nav'):
        nav=st.columns(5,gap='small')
        for col,(label,target) in zip(nav,[('⌂ 홈','home'),('▤ 내 기록','history'),('♡ 가족 소식','family'),('⚙ 설정','settings'),('ⓘ 안내','help')]):
            if col.button(label,key='nav_'+target,use_container_width=True,type='primary' if page==target else 'secondary'):
                if target=='home':
                    st.session_state.token=uuid.uuid4().hex
                    st.session_state.pop('part',None)
                if target=='help': st.session_state.help_return=page
                go(target)
    if page=='help': help_screen(); return
    # 재배포 뒤 세션만 남은 경우 존재하지 않는 가족으로 저장하지 않도록 처리.
    with db() as con:
        exists=con.execute('SELECT 1 FROM families WHERE id=?',(fid,)).fetchone()
    if not exists:
        st.session_state.clear(); st.warning('가족 공간을 다시 연결해 주세요.'); return
    if page in ('body','parts','region','level','done') and not st.session_state.get('who'): go('choose')
    if page=='level' and not st.session_state.get('part'): go('body')
    if page=='home': home_screen(fid)
    elif page=='setup': setup_screen(fid)
    elif page=='choose': choose_screen(fid)
    elif page=='body': body_screen(fid)
    elif page=='parts': parts_screen()
    elif page=='region': region_screen()
    elif page=='level': level_screen(fid)
    elif page=='done': done_screen(fid)
    elif page=='family': family_screen(fid)
    elif page=='history': history_screen(fid)
    elif page=='settings': settings_screen(fid)
    elif page=='qr': qr_screen(fid)
    elif page=='delete':
        st.subheader('이 기록을 삭제할까요?')
        st.caption('삭제하면 되돌릴 수 없어요.')
        if st.button('삭제하기',type='primary',use_container_width=True):
            remove(fid,st.session_state.get('delete_id','')); go('history')
        button('취소','history')
    elif page=='logout':
        st.subheader('로그아웃할까요?')
        if st.button('로그아웃',use_container_width=True): st.session_state.clear(); st.rerun()
        button('취소','settings')
    else: go('home')

if __name__=='__main__':
    try:
        main()
    except sqlite3.Error:
        st.error('저장소 연결에 문제가 있습니다. 저장 완료 화면이 없으면 기록 여부를 다시 확인해 주세요.')
