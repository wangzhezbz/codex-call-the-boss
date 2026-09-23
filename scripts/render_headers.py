"""Render static, self-contained SVG headers for the five README editions."""
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COPY = {
    'en': ('EXPERIMENTAL', 'The work is done.', 'Your phone rings.', 'A short report. Your next instruction. The same Codex task.', 'TASK-SCOPED PHONE REPORTS', 'Setup required'),
    'zh-CN': ('实验阶段', '任务做完，', '电话响起。', '简短汇报，接收下一步指令，回到原任务执行。', 'CODEX · 任务级电话汇报', '首次使用需要配置'),
    'ru': ('ЭКСПЕРИМЕНТ', 'Работа готова.', 'Вам звонит Codex.', 'Краткий отчёт. Новая команда. Та же задача Codex.', 'ЗВОНКИ ДЛЯ ВАШЕЙ ЗАДАЧИ', 'Нужна настройка'),
    'ja': ('実験段階', '作業が終わる。', '電話が鳴る。', '短く報告。次の指示を受けて、元のタスクで実行。', 'CODEX · タスクごとの電話報告', '初期設定が必要'),
    'ko': ('실험 단계', '작업이 끝나면', '전화가 울립니다.', '짧은 보고. 다음 지시. 원래 Codex 작업에서 실행.', 'CODEX · 작업별 전화 보고', '초기 설정 필요'),
}

for lang, copy in COPY.items():
    badge, first, second, sub, eyebrow, setup = map(escape, copy)
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="550" viewBox="0 0 1280 550" role="img" aria-labelledby="title desc">
<title id="title">Call the Boss — {first} {second}</title><desc id="desc">{sub} macOS + iPhone. {setup}.</desc>
<defs><linearGradient id="panel" x2="1" y2="1"><stop stop-color="#10151b"/><stop offset="1" stop-color="#1b222a"/></linearGradient></defs>
<rect x="1" y="1" width="1278" height="548" rx="30" fill="url(#panel)" stroke="#333e49" stroke-width="2"/>
<g fill="none" stroke="#28343d"><circle cx="1080" cy="280" r="190"/><circle cx="1080" cy="280" r="239"/><path d="M820 455H1230M1040 65V464"/></g>
<g font-family="Arial, 'Noto Sans', 'PingFang SC', 'Hiragino Sans', 'Apple SD Gothic Neo', sans-serif">
<rect x="48" y="42" width="44" height="44" rx="12" fill="#dceee9"/>
<path d="M60 54c-4 9 4 22 15 23l5-6-7-4-3 3c-3-2-5-4-6-7l3-3-4-7z" fill="#152129"/>
<text x="109" y="72" font-size="27" font-weight="700" fill="#f1f5f8">Call the Boss</text>
<rect x="981" y="48" width="248" height="32" rx="16" fill="#253137" stroke="#405051"/>
<circle cx="1000" cy="64" r="4" fill="#b7d6ca"/><text x="1015" y="69" font-size="12" font-weight="700" fill="#dceee9">{badge}</text>
<text x="58" y="150" font-size="15" letter-spacing="2" fill="#a9b8c5">{eyebrow}</text>
<text x="54" y="235" font-size="64" font-weight="700" fill="#f4f6f8">{first}</text>
<text x="54" y="315" font-size="64" font-weight="700" fill="#d2e9e0">{second}</text>
<text x="58" y="368" font-size="20" fill="#b0bdc9">{sub}</text>
<path d="M895 320h26v-32h15v64h15v-100h15v138h15v-110h15v61h15v-35h18" fill="none" stroke="#a9cabe" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>
<circle cx="1080" cy="280" r="13" fill="#b7d6ca"/><circle cx="1080" cy="280" r="25" fill="none" stroke="#537267"/>
<path d="M58 446H1222" stroke="#3a454e"/>
<text x="58" y="490" font-size="16" fill="#dce6ec">macOS + iPhone</text>
</g></svg>'''
    path = ROOT / 'docs/assets' / f'hero-{lang}.svg'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(svg, encoding='utf-8')
