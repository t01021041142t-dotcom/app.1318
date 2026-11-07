"""오늘 어디 아파? | streamlit run app.py
GitHub: 이 파일과 requirements.txt를 올려 Streamlit Community Cloud에서 배포.
가족 공간 생성 후 가족 ID와 비밀번호를 별도 전달. QR에는 가족 ID만 포함.
알림은 앱 내 표시이며 백그라운드 푸시/SMS나 응급 감시 서비스가 아닙니다.
SQLite 로컬 저장소는 Cloud 재시작/재배포 시 보존되지 않을 수 있습니다.
영구 운영 전에는 관리형 DB, 접근 통제, 개인정보 처리·보관 정책이 필요합니다.
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
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import qrcode
import streamlit as st
import plotly.graph_objects as go

DB_PATH = os.environ.get('CARE_DB_PATH', 'family_care.sqlite3')
TZ = ZoneInfo('Asia/Seoul')
PARTS = ['머리', '목', '왼쪽 어깨', '오른쪽 어깨', '가슴', '배', '허리', '왼쪽 팔·손', '오른쪽 팔·손', '왼쪽 무릎', '오른쪽 무릎', '왼쪽 발·발목', '오른쪽 발·발목']
SYMPTOMS = ['어지러움', '메스꺼움', '속쓰림·소화 불편', '기침', '목 따가움', '콧물·코막힘', '열감·오한', '피로·기운 없음', '잠을 잘 못 잠', '가려움', '설사', '변비']
LEVELS = {1: '🙂 살짝', 2: '😟 중간', 3: '😣 많이'}
DISCLAIMER = '이 앱은 진단·치료·응급 감시 도구가 아닙니다. 3단계는 간편 불편도이며, 임상 NPRS(0~10) 또는 VAS 점수가 아닙니다.'


def now():
    return datetime.now(TZ)


def connect():
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    return con


def init_db():
    with connect() as con:
        con.execute('CREATE TABLE IF NOT EXISTS families (id TEXT PRIMARY KEY, salt TEXT NOT NULL, digest TEXT NOT NULL)')
        con.execute('''CREATE TABLE IF NOT EXISTS entries (
            id TEXT PRIMARY KEY, family TEXT NOT NULL REFERENCES families(id),
            person TEXT NOT NULL, part TEXT NOT NULL, level INTEGER NOT NULL CHECK(level BETWEEN 0 AND 3),
            stamp TEXT NOT NULL, seen INTEGER NOT NULL DEFAULT 0)''')
        con.execute('CREATE INDEX IF NOT EXISTS idx_family_time ON entries(family, stamp)')


def digest(password, salt):
    return hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 260000).hex()


def create_family(password):
    fid = uuid.uuid4().hex
    salt = secrets.token_hex(16)
    with connect() as con:
        con.execute('INSERT INTO families VALUES (?,?,?)', (fid, salt, digest(password, salt)))
    return fid


def authenticate(fid, password):
    with connect() as con:
        row = con.execute('SELECT * FROM families WHERE id=?', (fid,)).fetchone()
    return bool(row and hmac.compare_digest(row['digest'], digest(password, row['salt'])))


def get_entries(fid, person=None):
    with connect() as con:
        if person is None:
            rows = con.execute('SELECT * FROM entries WHERE family=? ORDER BY stamp DESC, rowid DESC', (fid,)).fetchall()
        else:
            rows = con.execute('SELECT * FROM entries WHERE family=? AND person=? ORDER BY stamp DESC, rowid DESC', (fid, person)).fetchall()
    return [dict(r) for r in rows]


def valid_item(part):
    return isinstance(part, str) and (
        part in PARTS or part in SYMPTOMS
        or (part.startswith('직접 입력: ') and 0 < len(part[7:].strip()) <= 60)
    )


def add_entry(fid, person, part, level):
    if not person.strip() or len(person.strip()) > 20:
        raise ValueError('이름은 1~20자로 입력해 주세요.')
    if (level == 0 and part != '불편 없음') or (level != 0 and (not valid_item(part) or level not in (1, 2, 3))):
        raise ValueError('올바르지 않은 기록입니다.')
    eid = uuid.uuid4().hex
    with connect() as con:
        con.execute('INSERT INTO entries VALUES (?,?,?,?,?,?,0)', (eid, fid, person.strip(), part, level, now().isoformat()))
    return eid


def consecutive_days(rows, person, part, today):
    dates = {datetime.fromisoformat(r['stamp']).astimezone(TZ).date() for r in rows if r['person'] == person and r['part'] == part and r['level'] > 0}
    # 오늘 아직 기록하지 않은 경우 어제까지의 연속 기록도 표시.
    cursor = today if today in dates else today - timedelta(days=1)
    count = 0
    while cursor in dates:
        count += 1
        cursor -= timedelta(days=1)
    return count


def signals(rows, today):
    result = []
    pairs = {(r['person'], r['part']) for r in rows if r['level'] > 0}
    for person, part in sorted(pairs):
        recent = [r for r in rows if r['person'] == person and r['part'] == part and r['level'] > 0]
        days = consecutive_days(rows, person, part, today)
        high = any(r['level'] == 3 and datetime.fromisoformat(r['stamp']).astimezone(TZ).date() == today for r in recent)
        if high or days >= 3:
            reason = ('오늘 ‘많이’ 기록' if high else '') + (' · ' if high and days >= 3 else '') + (f'{days}일 연속 기록' if days >= 3 else '')
            result.append(f'{person} · {part}: {reason}')
    return result


def qr_url(base, fid):
    u = urlsplit(base.strip())
    if u.scheme != 'https' or not u.netloc or u.username or u.password:
        raise ValueError('https://로 시작하는 배포 주소를 입력해 주세요.')
    params = [(k, v) for k, v in parse_qsl(u.query) if k != 'family']
    return urlunsplit((u.scheme, u.netloc, u.path, urlencode(params + [('family', fid)]), ''))


def stamp_label(stamp):
    return datetime.fromisoformat(stamp).astimezone(TZ).strftime('%m/%d %H:%M')


def main():
    st.set_page_config(page_title='오늘 어디 아파?', page_icon='💛', layout='centered')
    st.markdown('''<style>
    .block-container{max-width:740px;padding-top:1rem;padding-bottom:2rem}
    div.stButton>button{min-height:52px;border-radius:16px;font-size:1.05rem}
    h1{font-size:1.9rem!important} .stCaption{font-size:.9rem}
    </style>''', unsafe_allow_html=True)
    init_db()
    st.title('💛 오늘 어디 아파?')
    st.caption('짧게 표현하고, 가족이 함께 살펴요.')
    if 'family' not in st.session_state:
        st.info('처음 한 번 가족 공간을 만들고, 가족에게 ID와 비밀번호를 알려 주세요.')
        mode = st.radio('시작 방법', ['가족 공간 들어가기', '새 가족 공간 만들기'], horizontal=True)
        default = st.query_params.get('family', '')
        with st.form('login'):
            fid = st.text_input('가족 ID', value=default) if mode == '가족 공간 들어가기' else ''
            password = st.text_input('가족 비밀번호 (10자 이상 권장)', type='password')
            repeat = st.text_input('비밀번호 다시 입력', type='password') if mode == '새 가족 공간 만들기' else password
            submitted = st.form_submit_button('시작하기', use_container_width=True)
        if submitted:
            until = st.session_state.get('login_until')
            if until and now() < until:
                st.error('잠시 후 다시 시도해 주세요.')
            elif mode == '새 가족 공간 만들기':
                if len(password) < 10 or password != repeat:
                    st.error('비밀번호는 10자 이상이며 두 입력이 같아야 합니다.')
                else:
                    st.session_state.family = create_family(password)
                    st.session_state.just_created = True
                    st.rerun()
            elif authenticate(fid.strip(), password):
                st.session_state.family = fid.strip()
                st.session_state.login_failures = 0
                st.rerun()
            else:
                n = st.session_state.get('login_failures', 0) + 1
                st.session_state.login_failures = n
                if n >= 5:
                    st.session_state.login_until = now() + timedelta(seconds=30)
                st.error('가족 ID 또는 비밀번호를 확인해 주세요. 서버 초기화 후에는 기존 공간이 없을 수 있습니다.')
        st.caption('비밀번호 복구 기능이 없습니다. QR만으로는 접속할 수 없으며 가족 비밀번호도 필요합니다. 로그인 시도 제한은 현재 브라우저 세션에만 적용됩니다.')
        st.caption(DISCLAIMER)
        return

    fid = st.session_state.family
    if st.session_state.pop('just_created', False):
        st.success('가족 공간을 만들었습니다. 아래 ‘가족 연결·백업’에서 ID를 보관하세요.')
    with st.expander('🔗 가족 연결 · 백업 · 사용 안내'):
        st.write('가족 ID (가족에게만 전달)')
        st.code(fid, language=None)
        st.caption('QR에는 비밀번호와 건강 기록이 들어가지 않습니다. 비밀번호는 따로 전달하세요.')
        base = st.text_input('배포된 앱 주소', placeholder='https://your-app.streamlit.app')
        if st.button('가족 QR 만들기'):
            try:
                link = qr_url(base, fid)
                buf = io.BytesIO()
                qrcode.make(link).save(buf, format='PNG')
                st.image(buf.getvalue(), width=190)
                st.code(link, language=None)
                st.download_button('QR 저장', buf.getvalue(), 'family_qr.png', 'image/png')
            except ValueError as e:
                st.error(str(e))
        backup = json.dumps({'format': 1, 'entries': get_entries(fid)}, ensure_ascii=False, indent=2)
        st.download_button('기록 JSON 백업', backup.encode('utf-8'), 'care_backup.json', 'application/json')
        st.warning('현재 저장은 서버 로컬 SQLite입니다. 서버 초기화·재배포 시 유실될 수 있습니다. 장기 운영 전 영구 DB 연결이 필요합니다. 백업 파일에는 건강정보가 포함되니 안전하게 보관하세요.')
        st.caption('자녀 알림은 앱을 열었을 때 표시됩니다. 자동 푸시·SMS·119 호출은 없습니다. 자녀 화면의 새로고침 버튼으로 최신 기록을 확인하세요. 기기 크기에 따라 스크롤이 필요할 수 있습니다.')
        st.caption(DISCLAIMER)
        if st.button('로그아웃'):
            st.session_state.clear()
            st.rerun()
    view = st.radio('사용 화면', ['🧓 부모님 기록', '👪 가족 확인'], horizontal=True, label_visibility='collapsed')
    flash = st.session_state.pop('flash', None)
    if flash:
        st.success(flash)
    st.warning('갑작스러운 심한 가슴 통증·숨참, 한쪽 마비·말 어눌함, 의식 변화 등 응급상황이 의심되면 기록보다 119 연락이 먼저입니다. 가족의 앱 확인을 기다리지 마세요.')

    if view == '🧓 부모님 기록':
        person = st.text_input('기록할 분의 이름·별명 (처음 입력 후 유지)', key='person', max_chars=20, placeholder='예: 엄마')
        selected = st.session_state.get('selected_part')
        if selected:
            st.subheader(f'② {selected} · 얼마나 불편하세요?')
            st.caption('진단용 점수가 아닌, 본인이 느끼는 불편 정도입니다.')
            for level, col in zip((1, 2, 3), st.columns(3)):
                if col.button(LEVELS[level], key=f'level_{level}', use_container_width=True):
                    if not person.strip():
                        st.error('위에 이름·별명을 먼저 입력해 주세요.')
                    else:
                        eid = add_entry(fid, person, selected, level)
                        st.session_state.last_id = eid
                        st.session_state.pop('selected_part', None)
                        st.session_state.flash = f'{person.strip()} · {selected} 기록 완료! 가족 화면에서 확인할 수 있어요.'
                        st.rerun()
            if st.button('← 다른 부위 선택', use_container_width=True):
                st.session_state.pop('selected_part', None)
                st.rerun()
        else:
            st.subheader('① 불편한 곳을 눌러 주세요')
            st.caption('본인 기준 왼쪽·오른쪽입니다. 허리는 뒷면에 해당합니다. 버튼 한 번 + 불편도 한 번으로 기록합니다.')
            # 본인 왼쪽은 정면 그림의 화면 오른쪽. 큰 선택점과 별도 접근성 버튼 제공.
            positions = [(0,9), (0,7.8), (1.1,7.2), (-1.1,7.2), (0,6.8), (0,5.5), (2.5,5.5), (1.7,5.8), (-1.7,5.8), (.65,3), (-.65,3), (.7,1), (-.7,1)]
            fig = go.Figure()
            # 배경 실루엣은 아래 레이어에 고정하여 선택점을 가리지 않음.
            from PIL import Image, ImageDraw
            silhouette = Image.new('RGBA', (420, 740), (255, 255, 255, 0))
            draw = ImageDraw.Draw(silhouette)
            fill = '#DCECEE'
            outline = '#A8C9CD'
            draw.ellipse((157, 14, 263, 122), fill=fill, outline=outline, width=3)
            draw.rounded_rectangle((186, 108, 234, 163), radius=18, fill=fill)
            draw.rounded_rectangle((127, 141, 293, 390), radius=57, fill=fill, outline=outline, width=3)
            draw.line([(143, 169), (105, 240), (76, 365)], fill=outline, width=46)
            draw.line([(143, 169), (105, 240), (76, 365)], fill=fill, width=40)
            draw.line([(277, 169), (315, 240), (344, 365)], fill=outline, width=46)
            draw.line([(277, 169), (315, 240), (344, 365)], fill=fill, width=40)
            for x in (139, 223):
                draw.rounded_rectangle((x, 351, x + 59, 686), radius=28, fill=fill, outline=outline, width=3)
                draw.rounded_rectangle((x - 6, 662, x + 67, 714), radius=20, fill=fill, outline=outline, width=3)
            fig.add_layout_image(dict(source=silhouette, xref='x', yref='y', x=-2.15, y=9.85, sizex=4.3, sizey=9.7, sizing='stretch', layer='below'))
            fig.add_trace(go.Scatter(x=[p[0] for p in positions], y=[p[1] for p in positions], mode='markers+text', text=[p.replace('왼쪽 ', '왼 ').replace('오른쪽 ', '오른 ') for p in PARTS], textposition='middle center', textfont=dict(size=12,color='#153F47'), customdata=PARTS, marker=dict(size=47,color='#FFF6D5',line=dict(color='#45838A',width=1)), selected=dict(marker=dict(color='#FDBF47')), unselected=dict(marker=dict(opacity=1)), hovertemplate='%{customdata}<extra></extra>'))
            fig.update_layout(height=450,margin=dict(l=5,r=5,t=5,b=5),showlegend=False,plot_bgcolor='white',paper_bgcolor='white',clickmode='event+select',dragmode=False,xaxis=dict(visible=False,range=[-3.2,3.2],fixedrange=True),yaxis=dict(visible=False,range=[0,10],fixedrange=True,scaleanchor='x',scaleratio=1))
            event = st.plotly_chart(fig, key=f"body_{st.session_state.get('map_generation', 0)}", on_select='rerun', selection_mode='points', use_container_width=True, config={'displayModeBar':False,'scrollZoom':False})
            if event.selection.points:
                point = event.selection.points[-1]
                part = point.get('customdata')
                if part in PARTS:
                    st.session_state.selected_part = part
                    st.session_state.map_generation = st.session_state.get('map_generation', 0) + 1
                    st.rerun()
            with st.expander('그림 선택이 어려우면 부위 버튼으로 선택'):
                for start in range(0, len(PARTS), 2):
                    for part, col in zip(PARTS[start:start+2], st.columns(2)):
                        if col.button(part, key=f'part_{part}', use_container_width=True):
                            st.session_state.selected_part = part
                            st.rerun()
            st.markdown('### 다른 증상·컨디션')
            st.caption('부위로 표현하기 어려운 증상을 고르거나 직접 적어 주세요. 선택 후 불편도를 누르면 저장됩니다.')
            for start in range(0, len(SYMPTOMS), 2):
                for symptom, col in zip(SYMPTOMS[start:start + 2], st.columns(2)):
                    if col.button(symptom, key=f'symptom_{symptom}', use_container_width=True):
                        st.session_state.selected_part = symptom
                        st.session_state.map_generation = st.session_state.get('map_generation', 0) + 1
                        st.rerun()
            with st.form('custom_symptom', clear_on_submit=True):
                custom = st.text_input('목록에 없는 증상 직접 쓰기', max_chars=60, placeholder='예: 입안이 헐어서 불편해요')
                custom_submit = st.form_submit_button('이 증상으로 불편도 선택', use_container_width=True)
            if custom_submit:
                if not custom.strip():
                    st.error('증상을 적어 주세요.')
                else:
                    st.session_state.selected_part = '직접 입력: ' + custom.strip()
                    st.session_state.map_generation = st.session_state.get('map_generation', 0) + 1
                    st.rerun()
            if st.button('😊 오늘은 불편한 곳 없어요', use_container_width=True):
                if not person.strip():
                    st.error('이름·별명을 먼저 입력해 주세요.')
                else:
                    st.session_state.last_id = add_entry(fid, person, '불편 없음', 0)
                    st.session_state.flash = '오늘의 편안한 상태를 기록했어요.'
                    st.rerun()
        if st.session_state.get('last_id'):
            if st.button('↩ 방금 기록 취소'):
                with connect() as con:
                    con.execute('DELETE FROM entries WHERE id=? AND family=?', (st.session_state.last_id, fid))
                st.session_state.pop('last_id', None)
                st.session_state.flash = '방금 기록을 취소했어요.'
                st.rerun()
        if person.strip():
            rows = get_entries(fid, person.strip())
            alerts = signals(rows, now().date())
            for alert in alerts:
                st.info(alert + '\n\n이는 연속 ‘기록’ 신호이며 실제 통증이 계속됐다는 판정은 아닙니다. 반복되거나 악화되는 불편은 의료진과 상담해 주세요.')
            with st.expander('최근 내 기록 · 안전 안내'):
                for r in rows[:10]:
                    st.write(f"{stamp_label(r['stamp'])} · {r['part']} · {LEVELS.get(r['level'], '😊 편안해요')}")
                st.write('새롭거나 심한 불편은 기간과 관계없이 상담이 필요할 수 있습니다. 편한 자세로 쉬되, 아픈 동작을 억지로 반복하지 마세요. 부위·점수만으로 온찜질/냉찜질이나 약을 결정하지 않습니다.')
                st.caption('‘불편 없음’ 기록은 이전 불편의 해소를 자동 판정하거나 가족 신호를 지우지 않습니다.')
    else:
        if st.button('🔄 최신 기록 확인', use_container_width=True):
            st.rerun()
        rows = get_entries(fid)
        today = now().date()
        today_rows = [r for r in rows if datetime.fromisoformat(r['stamp']).astimezone(TZ).date() == today]
        pending = [r for r in rows if not r['seen']]
        a, b, c = st.columns(3)
        a.metric('오늘 기록', len(today_rows))
        b.metric('미확인', len(pending))
        c.metric('확인 완료', len(rows) - len(pending))
        st.caption('가족 화면을 보는 것은 보호자의 실제 건강 확인을 대신하지 않습니다. ‘확인’은 기록을 읽었다는 뜻입니다.')
        alerts = signals(rows, today)
        if alerts:
            for alert in alerts:
                st.error('살펴봐 주세요 · ' + alert)
            st.caption('연속 일수는 한국 시간 기준 같은 부위·증상의 기록 날짜를 셉니다. 같은 날 여러 번 기록해도 하루로 계산합니다. 상태 악화 여부는 직접 연락해 확인하세요.')
        choice = st.selectbox('보여줄 기록', ['미확인', '오늘', '전체'])
        shown = pending if choice == '미확인' else today_rows if choice == '오늘' else rows
        if not shown:
            st.info('해당 기록이 없습니다.')
        if pending and st.button('미확인 기록 모두 읽음 처리'):
            # 현재 화면에 읽어 온 항목만 갱신: 동시에 새로 추가된 기록은 미확인 유지.
            with connect() as con:
                con.executemany('UPDATE entries SET seen=1 WHERE id=? AND family=?', [(r['id'], fid) for r in pending])
            st.rerun()
        page_count = max(1, (len(shown) + 4) // 5)
        page = st.selectbox('기록 페이지', range(1, page_count + 1))
        for r in shown[(page - 1) * 5:page * 5]:
            with st.container(border=True):
                st.write(f"**{r['person']} · {r['part']} · {LEVELS.get(r['level'], '😊 편안해요')}**")
                st.caption(stamp_label(r['stamp']) + (' · 확인 완료' if r['seen'] else ' · 미확인'))
                if not r['seen'] and st.button('읽었어요', key=f"seen_{r['id']}", use_container_width=True):
                    with connect() as con:
                        con.execute('UPDATE entries SET seen=1 WHERE id=? AND family=?', (r['id'], fid))
                    st.rerun()
        with st.expander('기록 삭제 (가족 공유 데이터)'):
            st.caption('삭제는 되돌릴 수 없습니다. 필요한 기록은 먼저 백업하세요.')
            if rows:
                options = {r['id']: f"{stamp_label(r['stamp'])} · {r['person']} · {r['part']}" for r in rows}
                target = st.selectbox('삭제할 기록', list(options), format_func=lambda x: options[x])
                consent = st.checkbox('선택한 기록을 가족 공간에서 영구 삭제합니다')
                if st.button('선택 기록 삭제', disabled=not consent):
                    with connect() as con:
                        con.execute('DELETE FROM entries WHERE id=? AND family=?', (target, fid))
                    st.session_state.flash = '삭제했어요.'
                    st.rerun()
    st.caption(DISCLAIMER)


if __name__ == '__main__':
    main()
