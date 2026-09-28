# -*- coding: utf-8 -*-
"""
Generator for COMPLETE_GOLDFLOW_CHAT_HISTORY.pdf on Desktop
Extracts ALL conversation turns from the beginning (Part 1: 42b9fa98 & Part 2: 4f0a4103)
into a beautifully styled PDF document saved directly to C:\\Users\\ckane\\Desktop\\
"""

import os
import json
import re
import html
import subprocess
import sys

# Reconfigure encoding
if sys.platform.startswith('win'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

def markdown_to_html(text):
    """Converts common markdown elements to clean HTML."""
    if not text:
        return ""
    
    # Escape HTML first
    text = html.escape(text)
    
    # Code blocks: ```lang ... ```
    def code_block_sub(m):
        code = m.group(2)
        lang = m.group(1) or ""
        return f'<pre class="code-block"><div class="code-lang">{lang}</div><code>{code}</code></pre>'
    
    text = re.sub(r'```(\w+)?\n(.*?)```', code_block_sub, text, flags=re.DOTALL)
    
    # Inline code: `code`
    text = re.sub(r'`([^`]+)`', r'<code class="inline-code">\1</code>', text)
    
    # Headers
    text = re.sub(r'^### (.*?)$', r'<h4 class="chat-h4">\1</h4>', text, flags=re.MULTILINE)
    text = re.sub(r'^## (.*?)$', r'<h3 class="chat-h3">\1</h3>', text, flags=re.MULTILINE)
    text = re.sub(r'^# (.*?)$', r'<h2 class="chat-h2">\1</h2>', text, flags=re.MULTILINE)
    
    # Bold & Italic
    text = re.sub(r'\*\*\*(.*?)\*\*\*', r'<strong><em>\1</em></strong>', text)
    text = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'\*(.*?)\*', r'<em>\1</em>', text)
    
    # Blockquotes
    text = re.sub(r'^> (.*?)$', r'<blockquote class="chat-quote">\1</blockquote>', text, flags=re.MULTILINE)
    
    # Unordered list items
    text = re.sub(r'^[ \t]*[\-\*] (.*?)$', r'<li class="chat-li">\1</li>', text, flags=re.MULTILINE)
    
    # Ordered list items
    text = re.sub(r'^[ \t]*(\d+)\. (.*?)$', r'<li class="chat-li-num"><strong>\1.</strong> \2</li>', text, flags=re.MULTILINE)
    
    # Table rows: | col1 | col2 | ...
    lines = text.split('\n')
    new_lines = []
    in_table = False
    table_rows = []
    
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('|') and stripped.endswith('|'):
            if not in_table:
                in_table = True
                table_rows = []
            table_rows.append(stripped)
        else:
            if in_table:
                new_lines.append(render_table(table_rows))
                in_table = False
                table_rows = []
            new_lines.append(line)
            
    if in_table:
        new_lines.append(render_table(table_rows))
        
    text = '\n'.join(new_lines)
    
    # Line breaks for standard paragraphs
    paragraphs = text.split('\n\n')
    processed_p = []
    for p in paragraphs:
        p_strip = p.strip()
        if not p_strip:
            continue
        if p_strip.startswith('<h') or p_strip.startswith('<pre') or p_strip.startswith('<blockquote') or p_strip.startswith('<table') or p_strip.startswith('<li'):
            processed_p.append(p_strip)
        else:
            p_formatted = p_strip.replace('\n', '<br>')
            processed_p.append(f'<p class="chat-p">{p_formatted}</p>')
            
    return '\n'.join(processed_p)

def render_table(rows):
    """Simple parser to render markdown table rows to HTML."""
    if not rows:
        return ""
    html_out = ['<table class="chat-table">']
    header_cols = [c.strip() for c in rows[0].strip('|').split('|')]
    html_out.append('<thead><tr>')
    for c in header_cols:
        html_out.append(f'<th>{c}</th>')
    html_out.append('</tr></thead><tbody>')
    
    start_idx = 1
    if len(rows) > 1 and re.match(r'^[\|\s\-\:]+$', rows[1]):
        start_idx = 2
        
    for r in rows[start_idx:]:
        cols = [c.strip() for c in r.strip('|').split('|')]
        html_out.append('<tr>')
        for c in cols:
            html_out.append(f'<td>{c}</td>')
        html_out.append('</tr>')
        
    html_out.append('</tbody></table>')
    return ''.join(html_out)

def parse_transcript_file(log_file, initial_turn_index=1):
    """Parses transcript.jsonl into structured conversation turns."""
    turns = []
    current_turn = None
    turn_index = initial_turn_index
    
    if not os.path.exists(log_file):
        return turns
        
    with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            try:
                item = json.loads(line)
                t = item.get('type')
                if t == 'USER_INPUT':
                    raw = item.get('content', '')
                    if '<USER_REQUEST>' in raw:
                        req_match = re.search(r'<USER_REQUEST>(.*?)</USER_REQUEST>', raw, re.DOTALL)
                        clean_text = req_match.group(1).strip() if req_match else raw.strip()
                    else:
                        clean_text = re.sub(r'<ADDITIONAL_METADATA>.*?</ADDITIONAL_METADATA>', '', raw, flags=re.DOTALL).strip()
                        clean_text = re.sub(r'<context>.*?</context>', '', clean_text, flags=re.DOTALL).strip()
                    
                    if not clean_text:
                        clean_text = "(No text / Context Trigger)"
                        
                    current_turn = {
                        'turn_number': turn_index,
                        'step': item.get('step_index'),
                        'user_text': clean_text,
                        'responses': []
                    }
                    turns.append(current_turn)
                    turn_index += 1
                elif t == 'PLANNER_RESPONSE' and current_turn is not None:
                    text = item.get('content', '').strip()
                    if text:
                        current_turn['responses'].append({
                            'step': item.get('step_index'),
                            'text': text
                        })
            except Exception:
                pass
                
    return turns

def build_html_document(all_turns):
    """Builds complete printable HTML for conversation history."""
    header_html = """<!DOCTYPE html>
<html lang="gu">
<head>
<meta charset="UTF-8">
<title>GOLDFLOW - સંપૂર્ણ ચેટ હિસ્ટ્રી (Complete Chat History)</title>
<style>
  @page {
    size: A4 portrait;
    margin: 14mm 10mm 14mm 10mm;
    @bottom-right {
      content: "GOLDFLOW Chat Archive • Page " counter(page) " of " counter(pages);
      font-size: 8pt;
      color: #94a3b8;
      font-family: 'Nirmala UI', 'Segoe UI', Arial, sans-serif;
    }
  }

  body {
    font-family: 'Nirmala UI', 'Segoe UI', -apple-system, BlinkMacSystemFont, Roboto, Arial, sans-serif;
    color: #1e293b;
    background: #ffffff;
    line-height: 1.5;
    font-size: 9pt;
    margin: 0;
    padding: 0;
  }

  .cover {
    page-break-after: always;
    background: linear-gradient(135deg, #0b132b 0%, #1c2541 60%, #2e1065 100%);
    color: #ffffff;
    padding: 45px 35px;
    border-radius: 10px;
    min-height: 860px;
    box-sizing: border-box;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
  }

  .cover h1 {
    font-size: 24pt;
    color: #facc15;
    margin: 0 0 8px 0;
    text-transform: uppercase;
    letter-spacing: 1px;
    border-bottom: 3px solid #facc15;
    padding-bottom: 10px;
  }

  .cover .sub {
    font-size: 12pt;
    color: #e2e8f0;
    margin-bottom: 20px;
  }

  .cover .guj-banner {
    background: rgba(250, 204, 21, 0.12);
    border: 1px solid #facc15;
    border-radius: 8px;
    padding: 16px;
    margin-bottom: 25px;
    color: #fef08a;
    font-size: 10pt;
    line-height: 1.6;
  }

  .cover .stats-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
    margin-top: 20px;
  }

  .cover .stat-card {
    background: rgba(255, 255, 255, 0.07);
    border: 1px solid rgba(255, 255, 255, 0.15);
    border-radius: 8px;
    padding: 12px;
  }

  .cover .stat-card h4 {
    margin: 0 0 4px 0;
    color: #38bdf8;
    font-size: 9pt;
    text-transform: uppercase;
  }

  .cover .stat-card p {
    margin: 0;
    color: #f8fafc;
    font-size: 8.8pt;
  }

  /* CHAT DIALOGUE LAYOUT */
  .turn-container {
    margin-bottom: 18px;
    page-break-inside: auto;
  }

  .turn-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    background: #f1f5f9;
    border: 1px solid #cbd5e1;
    border-radius: 6px;
    padding: 4px 10px;
    font-size: 8.5pt;
    font-weight: bold;
    color: #334155;
    margin-bottom: 6px;
  }

  .turn-badge {
    background: #0284c7;
    color: white;
    padding: 2px 8px;
    border-radius: 12px;
    font-size: 7.8pt;
  }

  /* USER BUBBLE */
  .user-bubble {
    background: #eff6ff;
    border: 1.5px solid #bfdbfe;
    border-left: 5px solid #2563eb;
    border-radius: 6px;
    padding: 9px 12px;
    margin-bottom: 10px;
    page-break-inside: avoid;
  }

  .user-title {
    display: flex;
    align-items: center;
    gap: 6px;
    font-weight: bold;
    font-size: 8.8pt;
    color: #1d4ed8;
    margin-bottom: 4px;
  }

  .user-content {
    font-size: 9.1pt;
    color: #1e3a8a;
    line-height: 1.5;
    white-space: pre-wrap;
    word-break: break-word;
  }

  /* ASSISTANT BUBBLE */
  .assistant-bubble {
    background: #ffffff;
    border: 1.5px solid #e2e8f0;
    border-left: 5px solid #10b981;
    border-radius: 6px;
    padding: 10px 14px;
    margin-bottom: 12px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.03);
    page-break-inside: auto;
  }

  .assistant-title {
    display: flex;
    align-items: center;
    gap: 6px;
    font-weight: bold;
    font-size: 8.8pt;
    color: #047857;
    margin-bottom: 6px;
    border-bottom: 1px solid #f0fdf4;
    padding-bottom: 4px;
  }

  .assistant-content {
    font-size: 8.9pt;
    color: #1e293b;
    line-height: 1.5;
    word-break: break-word;
  }

  /* TYPOGRAPHY INSIDE ASSISTANT */
  .chat-h2 {
    font-size: 10.5pt;
    color: #0f172a;
    border-bottom: 1.5px solid #cbd5e1;
    padding-bottom: 3px;
    margin: 12px 0 6px 0;
  }

  .chat-h3 {
    font-size: 9.8pt;
    color: #1e293b;
    margin: 10px 0 4px 0;
  }

  .chat-h4 {
    font-size: 9.2pt;
    color: #334155;
    margin: 8px 0 3px 0;
  }

  .chat-p {
    margin: 0 0 6px 0;
  }

  .code-block {
    background: #0f172a;
    color: #e2e8f0;
    padding: 8px 12px;
    border-radius: 6px;
    font-family: 'Consolas', 'Courier New', monospace;
    font-size: 8.2pt;
    overflow-x: auto;
    margin: 8px 0;
    position: relative;
    border: 1px solid #334155;
  }

  .code-lang {
    font-size: 7pt;
    color: #94a3b8;
    text-transform: uppercase;
    margin-bottom: 4px;
  }

  .inline-code {
    background: #f1f5f9;
    color: #0f172a;
    padding: 1px 4px;
    border-radius: 3px;
    font-family: 'Consolas', 'Courier New', monospace;
    font-size: 8.3pt;
    border: 1px solid #e2e8f0;
  }

  .chat-quote {
    border-left: 3px solid #94a3b8;
    background: #f8fafc;
    padding: 6px 10px;
    margin: 6px 0;
    color: #475569;
    font-style: italic;
  }

  .chat-li {
    margin-left: 16px;
    margin-bottom: 3px;
  }

  .chat-li-num {
    margin-left: 16px;
    margin-bottom: 3px;
  }

  .chat-table {
    width: 100%;
    border-collapse: collapse;
    margin: 8px 0;
    font-size: 8.3pt;
  }

  .chat-table th, .chat-table td {
    border: 1px solid #cbd5e1;
    padding: 4px 7px;
    text-align: left;
  }

  .chat-table th {
    background: #f1f5f9;
    font-weight: bold;
    color: #0f172a;
  }
</style>
</head>
<body>

<!-- COVER PAGE -->
<div class="cover">
  <div>
    <div style="font-size: 11pt; color: #38bdf8; font-weight: bold; letter-spacing: 2px; margin-bottom: 6px;">
      PROJECT GOLDFLOW // COMPLETE MASTER CONVERSATION ARCHIVE
    </div>
    <h1>GOLDFLOW™ સંપૂર્ણ ચેટ રેકોર્ડ</h1>
    <div class="sub">Complete End-to-End Pair-Programming Transcript (Day 1 to Present)</div>
    
    <div class="guj-banner">
      <strong>📌 સંપૂર્ણ ચેટ દસ્તાવેજ (Full Historical Record):</strong><br>
      આ પીડીએફ દસ્તાવેજમાં Goldflow પ્રોજેક્ટની શરૂઆત (તારીખ 02 સપ્ટેમ્બર 2026) થી લઈને આજ સુધી (21 સપ્ટેમ્બર 2026) ની એકેએક ચેટ, વપરાશકર્તાની દરેક સૂચના, આપેલા તમામ સવાલો, ટેકનિકલ ચર્ચાઓ, અલ્ગોરિધમ્સ, API કનેક્શન્સ, પોર્ટ્સ (8000, 8050, 8060, 8070, 8080, 8086, 8088, 8090, 8095) અને દરેક પ્રશ્નના જવાબો અક્ષરશઃ ક્રમબદ્ધ રીતે સંગ્રહિત છે.
    </div>

    <div class="stats-grid">
      <div class="stat-card">
        <h4>કુલ ચેટ ટર્ન્સ (Total Turns)</h4>
        <p><strong>""" + str(len(all_turns)) + """ Interactions</strong> (સંપૂર્ણ ક્રમિક વાર્તાલાપ)</p>
      </div>
      <div class="stat-card">
        <h4>સમયગાળો (Timeframe)</h4>
        <p><strong>02 સપ્ટેમ્બર 2026 થી 21 સપ્ટેમ્બર 2026</strong> (અવિરત હિસ્ટ્રી)</p>
      </div>
      <div class="stat-card">
        <h4>આવરી લીધેલા તમામ પોર્ટ્સ</h4>
        <p><strong>8000, 8050, 8060, 8070, 8080, 8086, 8088, 8090, 8095</strong></p>
      </div>
      <div class="stat-card">
        <h4>ડેટા ફીડ્સ અને APIs</h4>
        <p><strong>Equiti MT5, AllTick, iTick, TwelveData, RealMarket, Binance</strong></p>
      </div>
    </div>
  </div>

  <div style="border-top: 1px solid rgba(255,255,255,0.2); padding-top: 12px; display: flex; justify-content: space-between; font-size: 8.5pt; color: #94a3b8;">
    <span>ડેસ્કટોપ પર સેવ થયેલ ફાઇલ: <code>COMPLETE_GOLDFLOW_CHAT_HISTORY.pdf</code></span>
    <span>સુરક્ષિત આર્કાઇવ: 2026-09-21</span>
  </div>
</div>

<!-- CHAT TRANSCRIPT BODY -->
<div style="padding: 10px 5px;">
"""

    turns_html = []
    for turn in all_turns:
        t_num = turn["turn_number"]
        u_text = html.escape(turn["user_text"])
        
        turn_str = f"""
<div class="turn-container">
  <div class="turn-header">
    <span>CONVERSATION TURN #{t_num}</span>
    <span class="turn-badge">Step #{turn.get('step', t_num)}</span>
  </div>

  <div class="user-bubble">
    <div class="user-title">
      <span>👤 USER REQUEST (તમારો સવાલ / સૂચના)</span>
    </div>
    <div class="user-content">{u_text}</div>
  </div>
"""
        # Assistant Responses
        if turn["responses"]:
            for r in turn["responses"]:
                ans_html = markdown_to_html(r["text"])
                turn_str += f"""
  <div class="assistant-bubble">
    <div class="assistant-title">
      <span>🤖 ANTIGRAVITY ASSISTANT RESPONSE (જવાબ અને સોલ્યુશન)</span>
    </div>
    <div class="assistant-content">
      {ans_html}
    </div>
  </div>
"""
        else:
            turn_str += """
  <div class="assistant-bubble" style="border-left-color: #94a3b8;">
    <div class="assistant-content" style="color: #64748b; font-style: italic;">
      (Task executed / System actions processed)
    </div>
  </div>
"""
        turn_str += "</div>\n"
        turns_html.append(turn_str)

    footer_html = """
  <div style="text-align: center; margin-top: 30px; padding: 20px; border-top: 1px solid #cbd5e1; color: #64748b; font-size: 8.5pt;">
    — END OF GOLDFLOW MASTER CONVERSATION TRANSCRIPT ARCHIVE —
  </div>
</div>
</body>
</html>
"""

    return header_html + "".join(turns_html) + footer_html

def generate_pdf():
    desktop_dir = r"C:\Users\ckane\Desktop"
    project_dir = r"c:\Users\ckane\Desktop\goldflow1"
    
    log1 = r"C:\Users\ckane\.gemini\antigravity-ide\brain\42b9fa98-3d03-4f35-8516-0d237875d70a\.system_generated\logs\transcript.jsonl"
    log2 = r"C:\Users\ckane\.gemini\antigravity-ide\brain\4f0a4103-efb7-4748-913e-e5d4545ef80b\.system_generated\logs\transcript.jsonl"
    
    html_path = os.path.join(project_dir, "COMPLETE_GOLDFLOW_CHAT_HISTORY.html")
    desktop_pdf = os.path.join(desktop_dir, "COMPLETE_GOLDFLOW_CHAT_HISTORY.pdf")
    project_pdf = os.path.join(project_dir, "COMPLETE_GOLDFLOW_CHAT_HISTORY.pdf")
    chrome_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

    print("Parsing Part 1 transcript (42b9fa98)...")
    turns1 = parse_transcript_file(log1, initial_turn_index=1)
    print(f"Extracted {len(turns1)} turns from Part 1.")

    print("Parsing Part 2 transcript (4f0a4103)...")
    turns2 = parse_transcript_file(log2, initial_turn_index=len(turns1) + 1)
    print(f"Extracted {len(turns2)} turns from Part 2.")

    all_turns = turns1 + turns2
    print(f"TOTAL CONVERSATION TURNS: {len(all_turns)}")

    print("Building printable HTML transcript...")
    full_html = build_html_document(all_turns)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(full_html)
    print(f"Wrote HTML to {html_path} ({len(full_html)} chars)")

    # Run Chrome headless to compile PDF directly to Desktop
    cmd = [
        chrome_path,
        "--headless",
        "--disable-gpu",
        "--no-pdf-header-footer",
        f"--print-to-pdf={desktop_pdf}",
        html_path
    ]

    print(f"Running Chrome headless to compile PDF to Desktop: {desktop_pdf}...")
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode == 0 and os.path.exists(desktop_pdf):
        size_mb = os.path.getsize(desktop_pdf) / (1024 * 1024)
        print(f"✅ SUCCESS: Generated Desktop PDF at {desktop_pdf} ({size_mb:.2f} MB)")
        
        # Also copy/save a backup in project folder
        try:
            import shutil
            shutil.copy2(desktop_pdf, project_pdf)
            print(f"✅ Backup PDF saved at {project_pdf}")
        except Exception as e:
            print("Backup copy warning:", e)
    else:
        print("ERROR running Chrome:", res.stderr, res.stdout)

if __name__ == "__main__":
    generate_pdf()
