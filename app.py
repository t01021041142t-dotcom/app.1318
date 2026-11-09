"""오늘 어디 아파? / 실행: streamlit run app.py
GitHub에 app.py와 requirements.txt를 같은 위치에 업로드합니다.
SQLite는 로컬 시연용입니다. Cloud 초기화 시 데이터가 유실될 수 있습니다.
가족 비밀번호 소지자는 모든 구성원 기록을 조회·삭제할 수 있습니다.
자동 푸시/응급 감시/진단 기능 없음. 임상 NPRS 대신 3단계 주관적 불편도 사용.
"""
from __future__ import annotations
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
PARTS = ['머리','목','가슴','배','허리','왼쪽 어깨','오른쪽 어깨','왼쪽 팔·손','오른쪽 팔·손','왼쪽 엉덩이','오른쪽 엉덩이','왼쪽 무릎','오른쪽 무릎','왼쪽 발·발목','오른쪽 발·발목']

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
OUTLINE = 'M -.38,8.8 C -.85,8.82 -.9,9.8 -.48,10.08 C -.2,10.3 .2,10.3 .48,10.08 C .9,9.8 .85,8.82 .38,8.8 L .35,8.42 C .65,8.3 1.1,8.3 1.32,7.94 C 1.62,7.4 1.7,6.62 1.95,5.72 C 2.08,5.35 2.18,4.94 2.03,4.76 C 1.88,4.57 1.66,4.82 1.58,5.1 L 1.04,6.72 C .91,6.2 1.02,5.69 1.08,5.15 C 1.22,4.62 .98,3.65 .85,2.85 L .62,.85 C .92,.52 .96,.25 .67,.22 L .18,.22 C -.03,.22 .02,.56 .09,.91 L .18,3.05 L 0,4.45 L -.18,3.05 L -.09,.91 C -.02,.56 .03,.22 -.18,.22 L -.67,.22 C -.96,.25 -.92,.52 -.62,.85 L -.85,2.85 C -.98,3.65 -1.22,4.62 -1.08,5.15 C -1.02,5.69 -.91,6.2 -1.04,6.72 L -1.58,5.1 C -1.66,4.82 -1.88,4.57 -2.03,4.76 C -2.18,4.94 -2.08,5.35 -1.95,5.72 C -1.7,6.62 -1.62,7.4 -1.32,7.94 C -1.1,8.3 -.65,8.3 -.35,8.42 L -.38,8.8 Z'

def body_map():
    facing=st.segmented_control('신체 방향',['앞면','뒷면'],default='앞면',key='facing') or '앞면'
    # (부위,x,y,라벨x). 정면의 화면 오른쪽은 본인 왼쪽입니다.
    if facing=='앞면':
        pts=[('머리',0,9.5,2.9),('목',0,8.55,-2.9),('가슴',0,7.5,2.9),('배',0,6,-2.9),('왼쪽 어깨',1,8.05,2.9),('오른쪽 어깨',-1,8.05,-2.9),('왼쪽 팔·손',1.7,5.4,2.9),('오른쪽 팔·손',-1.7,5.4,-2.9),('왼쪽 무릎',.58,2.75,2.9),('오른쪽 무릎',-.58,2.75,-2.9),('왼쪽 발·발목',.48,.65,2.9),('오른쪽 발·발목',-.48,.65,-2.9)]
    else:
        pts=[('목',0,8.55,2.9),('허리',0,5.9,2.9),('왼쪽 어깨',-1,8,-2.9),('오른쪽 어깨',1,8,2.9),('왼쪽 팔·손',-1.7,5,-2.9),('오른쪽 팔·손',1.7,5,2.9),('왼쪽 엉덩이',-.6,4.5,-2.9),('오른쪽 엉덩이',.6,4.5,2.9),('왼쪽 무릎',-.58,2.8,-2.9),('오른쪽 무릎',.58,2.8,2.9),('왼쪽 발·발목',-.48,.65,-2.9),('오른쪽 발·발목',.48,.65,2.9)]
    fig=go_plot(pts)
    generation=st.session_state.get('map_gen',0)
    event=st.plotly_chart(fig,key=f'map_{generation}_{facing}',on_select='rerun',selection_mode='points',use_container_width=True,config={'displayModeBar':False,'scrollZoom':False})
    if event.selection.points:
        point=event.selection.points[-1]
        part=point.get('customdata')
        if isinstance(part,str) and part in PARTS:
            st.session_state.part=part
            st.session_state.map_gen=generation+1
            go('level')
    # 그림 터치가 불편한 기기를 위한 별도 기능 화면. 드롭다운/직접 입력 없음.
    button('부위 이름으로 선택', 'parts')

def go_plot(pts):
    fig=pg.Figure()
    fig.add_shape(type='path',path=OUTLINE,fillcolor='#DCE9EE',line={'color':'#93AFBC','width':1.7},layer='below')
    for part,x,y,lx in pts:
        fig.add_shape(type='line',x0=x,x1=lx*.82,y0=y,y1=y,line={'color':'#C4D3DC','width':1},layer='below')
    # 신체 부위와 글자 라벨 양쪽 모두 같은 선택 이벤트를 반환합니다.
    fig.add_trace(pg.Scatter(x=[p[1] for p in pts],y=[p[2] for p in pts],mode='markers',customdata=[p[0] for p in pts],marker={'size':24,'color':'#117F83','opacity':.85},hovertemplate='%{customdata}<extra></extra>'))
    fig.add_trace(pg.Scatter(x=[p[3] for p in pts],y=[p[2] for p in pts],mode='markers+text',text=[p[0].replace('왼쪽','왼').replace('오른쪽','오른') for p in pts],customdata=[p[0] for p in pts],textfont={'size':12,'color':'#18384B'},marker={'size':44,'color':'#F5F8FA','opacity':.95},hovertemplate='%{customdata}<extra></extra>'))
    fig.update_layout(height=420,showlegend=False,clickmode='event+select',dragmode=False,margin={'l':0,'r':0,'t':2,'b':0},paper_bgcolor='white',plot_bgcolor='white',xaxis={'visible':False,'range':[-4.05,4.05],'fixedrange':True},yaxis={'visible':False,'range':[0,10.5],'fixedrange':True})
    return fig

def login_screen():
    st.subheader('우리 가족 연결')
    mode=st.segmented_control('접속 방법',['들어가기','처음 만들기'],default='들어가기') or '들어가기'
    with st.form('login'):
        fid=st.text_input('가족 ID',value=st.query_params.get('family','')) if mode=='들어가기' else ''
        password=st.text_input('비밀번호',type='password')
        again=st.text_input('비밀번호 확인',type='password') if mode=='처음 만들기' else password
        submitted=st.form_submit_button('가족 공간 만들기' if mode=='처음 만들기' else '들어가기',use_container_width=True)
    if submitted:
        blocked=st.session_state.get('blocked')
        if blocked and now()<blocked:
            st.error('30초 뒤 다시 시도해 주세요.')
            return
        if mode=='처음 만들기':
            if password!=again: st.error('비밀번호가 서로 달라요.'); return
            try: fid=create_family(password)
            except ValueError as e: st.error(str(e)); return
        elif not login(fid.strip(),password):
            n=st.session_state.get('failures',0)+1
            st.session_state.failures=n
            if n>=5: st.session_state.blocked=now()+timedelta(seconds=30)
            st.error('가족 ID와 비밀번호를 확인해 주세요.')
            return
        st.session_state.fid=fid.strip()
        st.session_state.failures=0
        go('home' if members(fid.strip()) else 'setup')
    if mode=='처음 만들기': st.caption('비밀번호 10자 이상 · 가족 ID는 자동 발급됩니다.')

def setup_screen(fid):
    back('settings' if members(fid) else 'home')
    st.subheader('가족 추가')
    with st.form('add_member'):
        name=st.text_input('이름·별명',max_chars=16,placeholder='예: 연주, 엄마, 아빠')
        ok=st.form_submit_button('추가',use_container_width=True)
    if ok:
        try: add_member(fid,name)
        except ValueError as e: st.error(str(e))
        else: go('home')

def home_screen(fid):
    rows=records(fid)
    n=len([r for r in rows if not r['seen']])
    st.subheader('무엇을 할까요?')
    a,b=st.columns(2)
    with a: button('💛 내 상태 기록','choose')
    with b: button(f'👪 가족 확인 · {n}','family')
    a,b=st.columns(2)
    with a: button('📅 기록 돌아보기','history')
    with b: button('🔗 가족 연결','settings')
    if not members(fid):
        st.info('먼저 가족 이름을 등록해 주세요.')
        button('가족 이름 추가','setup')

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
    st.caption(f'{st.session_state.who}  ·  1 / 2')
    st.subheader('어디가 불편하세요?')
    body_map()
    if st.button('😊 오늘은 편안해요',use_container_width=True): complete(fid,'불편 없음',0)

def parts_screen():
    back('body')
    st.subheader('불편한 부위 선택')
    cols=st.columns(2)
    for i,part in enumerate(PARTS):
        if cols[i%2].button(part,key='p_'+part,use_container_width=True):
            st.session_state.part=part
            go('level')

def level_screen(fid):
    back('body')
    st.caption(f'{st.session_state.who}  ·  2 / 2')
    st.subheader(st.session_state.part)
    st.write('얼마나 불편하세요?')
    for level,emoji in [(1,'🙂'),(2,'😟'),(3,'😣')]:
        if st.button(f'{emoji}  {LEVELS[level]}  ·  저장',key=f'level{level}',use_container_width=True): complete(fid,st.session_state.part,level)

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
    st.write('**기록:** 가족 선택 → 부위 선택 → 불편도 선택 → 저장 완료')
    st.write('**가족 확인:** 새 기록을 하나씩 읽고 확인 버튼을 누르세요. 자동 푸시·문자 전송은 없습니다.')
    st.write('**주의 신호:** 오늘 ‘많이’ 기록 또는 같은 부위 3일 이상 연속 기록을 표시합니다. 같은 날은 한 번으로 계산하며, 실제 통증 지속·회복을 판정하지 않습니다.')
    st.write('3단계는 주관적 불편도입니다. 임상 NPRS(0~10)·VAS가 아니며 진단·치료에 사용하지 않습니다. 찜질·약 복용을 자동 추천하지 않습니다.')
    st.warning('갑작스러운 심한 가슴 통증·숨참, 한쪽 마비·말 어눌함, 의식 변화 등은 119에 연락하세요. 가족의 앱 확인을 기다리지 마세요.')
    st.write('가족 비밀번호를 가진 사람은 모든 가족의 기록을 조회·삭제할 수 있습니다. 구성원 동의를 받고 최소한의 정보만 입력하세요. 개인별 권한 인증은 없습니다.')
    st.write('로컬 SQLite 시연용: 서버 초기화·재배포 시 유실될 수 있습니다. 백업에는 건강정보가 포함됩니다. 백업 복원·비밀번호 복구는 지원하지 않습니다. 실제 운영 전 영구 DB와 개인정보 보호 보강이 필요합니다.')
    st.caption('로그인 제한은 현재 브라우저 세션에만 적용됩니다. 휴대폰 크기에 따라 스크롤이 필요합니다.')

def main():
    st.set_page_config(page_title='오늘 어디 아파?',page_icon='💛',layout='centered')
    st.markdown('''<style>
    .block-container{max-width:760px;padding-top:1.2rem;padding-bottom:1rem}
    div.stButton>button,div.stFormSubmitButton>button{min-height:54px;border-radius:16px;font-size:1.08rem}
    h1{font-size:clamp(1.35rem,5vw,2rem)!important} h3{font-size:1.35rem!important}
    [data-testid="stMetric"]{background:#F0F7F7;border-radius:16px;padding:12px}
    </style>''',unsafe_allow_html=True)
    initialize()
    a,b=st.columns([5,2])
    with a: st.title('💛 오늘 어디 아파?')
    with b:
        if st.button('ⓘ 안내',use_container_width=True):
            if st.session_state.get('page')!='help': st.session_state.help_return=st.session_state.get('page','home')
            go('help')
        st.caption('응급 시 119')
    page=st.session_state.get('page','home')
    if page=='help': help_screen(); return
    fid=st.session_state.get('fid')
    if not fid: login_screen(); return
    # 재배포 뒤 세션만 남은 경우 존재하지 않는 가족으로 저장하지 않도록 처리.
    with db() as con:
        exists=con.execute('SELECT 1 FROM families WHERE id=?',(fid,)).fetchone()
    if not exists:
        st.session_state.clear(); st.warning('가족 공간을 다시 연결해 주세요.'); return
    if page in ('body','parts','level','done') and not st.session_state.get('who'): go('choose')
    if page=='level' and not st.session_state.get('part'): go('body')
    if page=='home': home_screen(fid)
    elif page=='setup': setup_screen(fid)
    elif page=='choose': choose_screen(fid)
    elif page=='body': body_screen(fid)
    elif page=='parts': parts_screen()
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
