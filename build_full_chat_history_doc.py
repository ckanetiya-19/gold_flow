# -*- coding: utf-8 -*-
"""
Generator for FULL_CONVERSATION_CHAT_HISTORY.pdf
Extracts and exhaustively documents all conversation turns from start to end
from transcript.jsonl into a beautifully styled PDF document.
"""

import os
import json
import re
import html
import subprocess

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
                # Render accumulated table
                new_lines.append(render_table(table_rows))
                in_table = False
                table_rows = []
            new_lines.append(line)
            
    if in_table:
        new_lines.append(render_table(table_rows))
        
    text = '\n'.join(new_lines)
    
    # Line breaks for standard paragraphs (excluding blocks)
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
    
    # First row is usually header
    header_cols = [c.strip() for c in rows[0].strip('|').split('|')]
    html_out.append('<thead><tr>')
    for c in header_cols:
        html_out.append(f'<th>{c}</th>')
    html_out.append('</tr></thead><tbody>')
    
    # Check if row 1 is separator (---)
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

def parse_transcript(log_file):
    """Parses transcript.jsonl into structured conversation turns."""
    turns = []
    current_turn = None
    turn_index = 1
    
    with open(log_file, 'r', encoding='utf-8') as f:
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
            except Exception as e:
                pass
                
    return turns

def build_html_document(turns):
    """Builds complete printable HTML for conversation history."""
    header_html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Goldflow - Full Conversation Chat History</title>
<style>
  @page {
    size: A4 portrait;
    margin: 15mm 12mm 15mm 12mm;
    @bottom-right {
      content: "Chat Archive • Page " counter(page) " of " counter(pages);
      font-size: 8pt;
      color: #94a3b8;
      font-family: 'Segoe UI', Arial, sans-serif;
    }
  }

  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, 'Nirmala UI', sans-serif;
    color: #1e293b;
    background: #ffffff;
    line-height: 1.5;
    font-size: 9.2pt;
    margin: 0;
    padding: 0;
  }

  .cover {
    page-break-after: always;
    background: linear-gradient(135deg, #0b132b 0%, #1c2541 60%, #3a506b 100%);
    color: #ffffff;
    padding: 50px 40px;
    border-radius: 10px;
    min-height: 880px;
    box-sizing: border-box;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
  }

  .cover h1 {
    font-size: 26pt;
    color: #38bdf8;
    margin: 0 0 10px 0;
    text-transform: uppercase;
    letter-spacing: 1px;
    border-bottom: 3px solid #38bdf8;
    padding-bottom: 12px;
  }

  .cover .sub {
    font-size: 13pt;
    color: #cbd5e1;
    margin-bottom: 25px;
  }

  .cover .guj-banner {
    background: rgba(56, 189, 248, 0.1);
    border: 1px solid #38bdf8;
    border-radius: 8px;
    padding: 18px;
    margin-bottom: 30px;
    color: #e0f2fe;
    font-size: 10.5pt;
    line-height: 1.6;
  }

  .cover .stats-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 15px;
    margin-top: 25px;
  }

  .cover .stat-card {
    background: rgba(255, 255, 255, 0.06);
    border: 1px solid rgba(255, 255, 255, 0.12);
    border-radius: 8px;
    padding: 14px;
  }

  .cover .stat-card h4 {
    margin: 0 0 5px 0;
    color: #f59e0b;
    font-size: 9.5pt;
    text-transform: uppercase;
  }

  .cover .stat-card p {
    margin: 0;
    color: #f1f5f9;
    font-size: 9pt;
  }

  /* CHAT DIALOGUE LAYOUT */
  .turn-container {
    margin-bottom: 22px;
    page-break-inside: auto;
  }

  .turn-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    background: #f1f5f9;
    border: 1px solid #e2e8f0;
    border-radius: 6px;
    padding: 4px 10px;
    font-size: 8.5pt;
    font-weight: bold;
    color: #475569;
    margin-bottom: 8px;
  }

  .turn-badge {
    background: #0284c7;
    color: white;
    padding: 2px 8px;
    border-radius: 12px;
    font-size: 8pt;
  }

  /* USER BUBBLE */
  .user-bubble {
    background: #eff6ff;
    border: 1.5px solid #bfdbfe;
    border-left: 5px solid #2563eb;
    border-radius: 8px;
    padding: 10px 14px;
    margin-bottom: 12px;
    page-break-inside: avoid;
  }

  .user-title {
    display: flex;
    align-items: center;
    gap: 6px;
    font-weight: bold;
    font-size: 9pt;
    color: #1d4ed8;
    margin-bottom: 6px;
  }

  .user-content {
    font-size: 9.3pt;
    color: #1e3a8a;
    line-height: 1.55;
    white-space: pre-wrap;
    word-break: break-word;
  }

  /* ASSISTANT BUBBLE */
  .assistant-bubble {
    background: #ffffff;
    border: 1.5px solid #e2e8f0;
    border-left: 5px solid #10b981;
    border-radius: 8px;
    padding: 12px 16px;
    margin-bottom: 14px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.03);
    page-break-inside: auto;
  }

  .assistant-title {
    display: flex;
    align-items: center;
    gap: 6px;
    font-weight: bold;
    font-size: 9pt;
    color: #047857;
    margin-bottom: 8px;
    border-bottom: 1px solid #f0fdf4;
    padding-bottom: 4px;
  }

  .assistant-content {
    font-size: 9.1pt;
    color: #1e293b;
    line-height: 1.55;
    word-break: break-word;
  }

  /* TYPOGRAPHY INSIDE ASSISTANT */
  .chat-h2 {
    font-size: 11pt;
    color: #0f172a;
    border-bottom: 1.5px solid #cbd5e1;
    padding-bottom: 3px;
    margin: 12px 0 6px 0;
  }

  .chat-h3 {
    font-size: 10pt;
    color: #1e40af;
    margin: 10px 0 4px 0;
  }

  .chat-h4 {
    font-size: 9.3pt;
    color: #334155;
    margin: 8px 0 3px 0;
  }

  .chat-p {
    margin: 4px 0 8px 0;
  }

  .chat-quote {
    background: #f8fafc;
    border-left: 3px solid #64748b;
    margin: 6px 0;
    padding: 4px 10px;
    color: #475569;
    font-style: italic;
  }

  .chat-li {
    margin: 2px 0;
  }

  .chat-li-num {
    margin: 2px 0;
    list-style-type: none;
  }

  .code-block {
    background: #0f172a;
    color: #f8fafc;
    border-radius: 6px;
    padding: 10px 12px;
    margin: 8px 0;
    overflow-x: auto;
    font-family: 'Consolas', 'Courier New', monospace;
    font-size: 8.4pt;
    line-height: 1.45;
  }

  .code-lang {
    font-size: 7.5pt;
    color: #94a3b8;
    text-transform: uppercase;
    margin-bottom: 4px;
    border-bottom: 1px solid #334155;
    padding-bottom: 2px;
  }

  .inline-code {
    background: #f1f5f9;
    color: #0f172a;
    border: 1px solid #cbd5e1;
    padding: 1px 4px;
    border-radius: 4px;
    font-family: 'Consolas', monospace;
    font-size: 8.4pt;
  }

  .chat-table {
    width: 100%;
    border-collapse: collapse;
    margin: 10px 0;
    font-size: 8.5pt;
  }

  .chat-table th, .chat-table td {
    border: 1px solid #cbd5e1;
    padding: 5px 8px;
    text-align: left;
  }

  .chat-table th {
    background: #0f172a;
    color: #ffffff;
    font-weight: 600;
  }

  .chat-table tr:nth-child(even) {
    background: #f8fafc;
  }
</style>
</head>
<body>

<!-- COVER PAGE -->
<div class="cover">
  <div>
    <h1>GOLDFLOW COMPLETE CHAT HISTORY</h1>
    <div class="sub">CHRONOLOGICAL MASTER TRANSCRIPT (START TO END)</div>

    <div class="guj-banner">
      <strong>📜 સંપૂર્ણ ચર્ચા અને આદાન-પ્રદાન કાયમી રેકોર્ડ (Complete Permanent Record):</strong><br>
      આ ડોક્યુમેન્ટમાં Goldflow પ્રોજેક્ટની શરૂઆતથી લઈને આજ સુધી વપરાશકર્તા (User) અને Antigravity AI આસિસ્ટન્ટ વચ્ચે થયેલી તમામ ચર્ચાઓ, આપેલા પ્રશ્નો, જવાબો, ટેકનિકલ સૂચનો, કન્ફર્મેશન્સ, અને નિર્ણયોનો સંપૂર્ણ કાલક્રમિક (Chronological) રેકોર્ડ સંગ્રહિત છે. એકપણ સંવાદ કે વિગત છોડ્યા વગર અસલ શબ્દોમાં અહીં પ્રસ્તુત છે.
    </div>

    <div class="stats-grid">
      <div class="stat-card">
        <h4>Dialogue Volume</h4>
        <p>• <strong>Total User Turns:</strong> """ + str(len(turns)) + """ Turns<br>
           • <strong>Language Modes:</strong> Gujarati & English Bilingual<br>
           • <strong>Conversation Scope:</strong> Complete Project Lifecycle</p>
      </div>
      <div class="stat-card">
        <h4>Key Project Themes Discussed</h4>
        <p>• <strong>Port 8000:</strong> WebSocket Multi-source Data Engine<br>
           • <strong>Port 8050 & 8060:</strong> V1/V2 Order Flow, True VWAP, Bands<br>
           • <strong>Port 8070 & 8080:</strong> Bloomberg DOM, CHOP Filter, Manual Cut<br>
           • <strong>Port 8090:</strong> 5 AI Agents, Central Banks, TV Candlesticks</p>
      </div>
      <div class="stat-card">
        <h4>Core Operating Principles</h4>
        <p>• <strong>Zero Regression:</strong> Never alter a working port codebase.<br>
           • <strong>Explain First:</strong> Prioritize clear Gujarati explanations before making modifications.</p>
      </div>
      <div class="stat-card">
        <h4>Archive Verification</h4>
        <p>• <strong>Workspace:</strong> c:/Users/ckane/Desktop/goldflow1<br>
           • <strong>Transcript Source:</strong> antigravity-ide/brain/logs/transcript.jsonl<br>
           • <strong>Date Generated:</strong> September 2026</p>
      </div>
    </div>
  </div>

  <div style="font-size: 8.5pt; color: #94a3b8; border-top: 1px solid rgba(255,255,255,0.15); padding-top: 10px;">
    GOLDFLOW QUANTITATIVE TRADING INTELLIGENCE — OFFICIAL TRANSCRIPT ARCHIVE
  </div>
</div>

<div style="page-break-before: always; padding-top: 10px;">
  <div style="font-size: 15pt; font-weight: bold; color: #0f172a; border-bottom: 2px solid #0284c7; padding-bottom: 6px; margin-bottom: 16px;">
    Transcript Begins: Chronological Conversation Record
  </div>
"""

    turns_html = []
    for turn in turns:
        t_num = turn['turn_number']
        step_id = turn['step']
        u_text = html.escape(turn['user_text'])
        
        turn_str = f"""
<div class="turn-container">
  <div class="turn-header">
    <span>TURN #{t_num} (Step {step_id})</span>
    <span class="turn-badge">Verified Session</span>
  </div>

  <div class="user-bubble">
    <div class="user-title">
      <span>👤 TRADER / USER (ckane):</span>
    </div>
    <div class="user-content">{u_text}</div>
  </div>
"""
        # Append assistant responses
        if turn['responses']:
            for resp in turn['responses']:
                r_step = resp['step']
                raw_resp = resp['text']
                formatted_html = markdown_to_html(raw_resp)
                turn_str += f"""
  <div class="assistant-bubble">
    <div class="assistant-title">
      <span>🤖 ANTIGRAVITY AI TEAM (Step {r_step}):</span>
    </div>
    <div class="assistant-content">
      {formatted_html}
    </div>
  </div>
"""
        else:
            turn_str += """
  <div class="assistant-bubble" style="border-left-color: #94a3b8;">
    <div class="assistant-title" style="color: #64748b;">
      <span>⚙️ SYSTEM / INTERACTION PROCESSED:</span>
    </div>
    <div class="assistant-content" style="color: #64748b; font-style: italic;">
      (Turn registered and processed in system lifecycle)
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
    base_dir = r"c:\Users\ckane\Desktop\goldflow1"
    log_file = r"C:\Users\ckane\.gemini\antigravity-ide\brain\4f0a4103-efb7-4748-913e-e5d4545ef80b\.system_generated\logs\transcript.jsonl"
    html_path = os.path.join(base_dir, "FULL_CONVERSATION_CHAT_HISTORY.html")
    pdf_path = os.path.join(base_dir, "FULL_CONVERSATION_CHAT_HISTORY.pdf")
    chrome_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

    print("Parsing transcript log...")
    turns = parse_transcript(log_file)
    print(f"Extracted {len(turns)} conversation turns.")

    print("Building HTML transcript...")
    full_html = build_html_document(turns)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(full_html)
    print(f"Wrote HTML to {html_path} ({len(full_html)} chars)")

    cmd = [
        chrome_path,
        "--headless",
        "--disable-gpu",
        "--no-pdf-header-footer",
        f"--print-to-pdf={pdf_path}",
        html_path
    ]

    print("Running Chrome headless to compile chat PDF...")
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode == 0 and os.path.exists(pdf_path):
        size_mb = os.path.getsize(pdf_path) / (1024 * 1024)
        print(f"SUCCESS: Generated PDF at {pdf_path} ({size_mb:.2f} MB)")
    else:
        print("ERROR:", res.stderr, res.stdout)

if __name__ == "__main__":
    generate_pdf()
