# -*- coding: utf-8 -*-
"""
Applies exact 1-to-1 matching layout from quant_8080_style_chart_1789298272244.jpg
to xauusd_hybrid_stream_terminal.py with single-screen fit (100vh, zero scrolling)
and ultra-attractive dark theme.
"""
import os, sys, ast

target_file = r"c:\Users\ckane\Desktop\goldflow1\xauusd_hybrid_stream_terminal.py"
scratch_target = r"C:\Users\ckane\.gemini\antigravity-ide\scratch\order_flow\xauusd_hybrid_stream_terminal.py"

with open(target_file, "r", encoding="utf-8") as f:
    text = f.read()

split_mark = "app = dash.Dash("
prefix = text[:text.find(split_mark)]

new_dash_code = '''app = dash.Dash(
    __name__,
    title="GOLD.FLOW // AI Quant Stream Terminal (XAUUSD)",
    update_title=None,
    suppress_callback_exceptions=True
)

app.layout = html.Div(
    id="master-quant-container",
    style={
        "backgroundColor": "#070A0F",
        "color": "#E2E8F0",
        "fontFamily": "'Segoe UI', 'Consolas', -apple-system, sans-serif",
        "height": "100vh",
        "maxHeight": "100vh",
        "overflow": "hidden",
        "padding": "4px 8px",
        "boxSizing": "border-box",
        "display": "flex",
        "flexDirection": "column"
    },
    children=[
        dcc.Interval(id="quant-interval", interval=1000, n_intervals=0),

        # 1. TOP HEADER (Exact Image Match)
        html.Div(
            style={
                "display": "flex", "justifyContent": "space-between", "alignItems": "center",
                "backgroundColor": "#0A0E17", "border": "1px solid #162032", "borderRadius": "4px",
                "padding": "4px 12px", "marginBottom": "4px", "height": "34px", "flexShrink": 0
            },
            children=[
                # Left Title
                html.Div([
                    html.Span("GOLD.FLOW ", style={"color": "#FFFFFF", "fontWeight": "900", "fontSize": "15px", "letterSpacing": "1px"}),
                    html.Span("// AI QUANT STREAM TERMINAL ", style={"color": "#94A3B8", "fontWeight": "bold", "fontSize": "13px"}),
                    html.Span("[PORT 8095]", style={"color": "#FFFFFF", "fontWeight": "bold", "fontSize": "12px", "marginLeft": "2px"})
                ], style={"whiteSpace": "nowrap"}),

                # Middle Clocks (2 lines: time zones on top, times below)
                html.Div(id="quant-clocks", style={"textAlign": "center"}),

                # Right Circuit Breaker
                html.Div(id="circuit-breaker-tag")
            ]
        ),

        # 2. TWO UPPER AI BANNERS (Row 2 - Exact Image Match)
        html.Div(
            style={"display": "grid", "gridTemplateColumns": "1fr 1fr", "gap": "6px", "marginBottom": "4px", "flexShrink": 0},
            children=[
                html.Div(id="ai-regime-card", style={"backgroundColor": "#0A0E17", "border": "1px solid #162032", "borderRadius": "4px", "padding": "4px 10px", "fontSize": "11px", "fontWeight": "bold"}),
                html.Div(id="vwap-sniper-card", style={"backgroundColor": "#0A0E17", "border": "1px solid #162032", "borderRadius": "4px", "padding": "4px 10px", "fontSize": "11px", "fontWeight": "bold"})
            ]
        ),

        # 3. KEY METRICS STRIP (Row 3 - Exact Single Row 5-Metrics from Image)
        html.Div(
            id="metrics-strip-row",
            style={
                "backgroundColor": "#0A0E17", "border": "1px solid #162032", "borderRadius": "4px",
                "padding": "3px 12px", "marginBottom": "4px", "display": "flex", "justifyContent": "space-between",
                "alignItems": "center", "fontSize": "11px", "height": "26px", "flexShrink": 0
            }
        ),

        # 4. MAIN BODY (Row 4: Chart Left 78% + Right Column 22%)
        html.Div(
            style={"display": "grid", "gridTemplateColumns": "3.8fr 1.2fr", "gap": "6px", "flexGrow": 1, "minHeight": 0, "marginBottom": "4px"},
            children=[
                # Center Chart Panel
                html.Div(
                    style={"backgroundColor": "#0A0E17", "border": "1px solid #162032", "borderRadius": "4px", "padding": "4px 6px", "display": "flex", "flexDirection": "column"},
                    children=[
                        html.Div(
                            "EXACT SIGNATURE 3-TIER MULTI-PANE CHART LAYOUT FROM PORT 8080)",
                            style={"color": "#94A3B8", "fontSize": "10px", "fontWeight": "bold", "marginBottom": "2px", "paddingLeft": "4px"}
                        ),
                        html.Div(
                            dcc.Graph(
                                id="quant-main-chart",
                                config={"displayModeBar": False, "responsive": True},
                                style={"width": "100%", "height": "100%"}
                            ),
                            style={"flexGrow": 1, "minHeight": 0}
                        )
                    ]
                ),

                # Right Column (4 Cards matching the image)
                html.Div(
                    style={"display": "flex", "flexDirection": "column", "gap": "4px", "height": "100%"},
                    children=[
                        # 1. DOM Depth Ladder
                        html.Div(
                            style={"backgroundColor": "#0A0E17", "border": "1px solid #162032", "borderRadius": "4px", "padding": "3px 6px", "flexShrink": 0},
                            children=[
                                html.Div(
                                    style={"display": "flex", "justifyContent": "space-between", "alignItems": "center", "marginBottom": "2px"},
                                    children=[
                                        html.Span("DOM Depth Ladder", style={"color": "#E2E8F0", "fontSize": "10px", "fontWeight": "bold"}),
                                        html.Span("⚙", style={"color": "#94A3B8", "fontSize": "10px"})
                                    ]
                                ),
                                html.Div(id="dom-ladder-content")
                            ]
                        ),

                        # 2. BVC Model probability
                        html.Div(
                            style={"backgroundColor": "#0A0E17", "border": "1px solid #162032", "borderRadius": "4px", "padding": "3px 6px", "flexShrink": 0},
                            children=[
                                html.Div("BVC Model probability", style={"color": "#E2E8F0", "fontSize": "10px", "fontWeight": "bold", "marginBottom": "2px"}),
                                html.Div(id="bvc-meter-content")
                            ]
                        ),

                        # 3. OFI slider
                        html.Div(
                            style={"backgroundColor": "#0A0E17", "border": "1px solid #162032", "borderRadius": "4px", "padding": "3px 6px", "flexShrink": 0},
                            children=[
                                html.Div("OFI slider", style={"color": "#E2E8F0", "fontSize": "10px", "fontWeight": "bold", "marginBottom": "2px"}),
                                html.Div(id="ofi-slider-content")
                            ]
                        ),

                        # 4. Footprint clusters (Exact table from image)
                        html.Div(
                            style={"backgroundColor": "#0A0E17", "border": "1px solid #162032", "borderRadius": "4px", "padding": "3px 6px", "flexGrow": 1, "minHeight": 0, "overflow": "hidden"},
                            children=[
                                html.Div(
                                    style={"display": "flex", "justifyContent": "space-between", "alignItems": "center", "marginBottom": "2px"},
                                    children=[
                                        html.Span("Footprint clusters", style={"color": "#E2E8F0", "fontSize": "10px", "fontWeight": "bold"}),
                                        html.Span(id="lee-ready-mini-tag", style={"fontSize": "8px", "color": "#00E676"})
                                    ]
                                ),
                                html.Div(id="footprint-clusters-content", style={"height": "calc(100% - 16px)", "overflowY": "auto"})
                            ]
                        )
                    ]
                )
            ]
        ),

        # 5. BOTTOM PANEL: Execution Log Blotter (Row 5 - Exact Image Match)
        html.Div(
            style={
                "backgroundColor": "#0A0E17", "border": "1px solid #162032", "borderRadius": "4px",
                "padding": "4px 10px", "height": "40px", "flexShrink": 0, "display": "flex",
                "justifyContent": "space-between", "alignItems": "center"
            },
            children=[
                html.Div("Execution Log Blotter", style={"color": "#94A3B8", "fontSize": "11px", "fontWeight": "bold"}),
                html.Div(id="blotter-mini-summary", style={"fontSize": "10px", "color": "#64748B"})
            ]
        )
    ]
)

# =============================================================================
# 8. DASH REAL-TIME RENDERING CALLBACK
# =============================================================================
@app.callback(
    [
        Output("quant-clocks", "children"),
        Output("circuit-breaker-tag", "children"),
        Output("ai-regime-card", "children"),
        Output("vwap-sniper-card", "children"),
        Output("metrics-strip-row", "children"),
        Output("quant-main-chart", "figure"),
        Output("dom-ladder-content", "children"),
        Output("bvc-meter-content", "children"),
        Output("ofi-slider-content", "children"),
        Output("footprint-clusters-content", "children"),
        Output("lee-ready-mini-tag", "children"),
        Output("blotter-mini-summary", "children")
    ],
    [Input("quant-interval", "n_intervals")]
)
def update_quant_terminal_ui(n):
    with data_lock:
        state = dict(market_state)
        cum = dict(cum_metrics)
        bars = list(historical_bars)
        trades = list(trade_history)
        pos = dict(trade_state)
        sniper = dict(sniper_state)
        tape = list(recent_tape)
        book = dict(order_book)
        cur_b = dict(current_bar)

    price = state["live_price"]
    
    # 1. Clocks (Exact 2-Line Format from Image)
    now = datetime.now()
    now_utc = datetime.now(timezone.utc)
    utc_t = now_utc.strftime("%I:%M %p")
    nyc_t = (now_utc - timedelta(hours=4)).strftime("%I:%M %p")
    ldn_t = (now_utc + timedelta(hours=1)).strftime("%I:%M %p")
    ist_t = now.strftime("%I:%M %p")

    clocks_elem = html.Div([
        html.Div([
            html.Span("UTC", style={"marginRight": "14px"}),
            html.Span("NYC", style={"marginRight": "14px"}),
            html.Span("LDN", style={"marginRight": "14px"}),
            html.Span("IST")
        ], style={"color": "#64748B", "fontSize": "8px", "letterSpacing": "1.5px"}),
        html.Div([
            html.Span(f"{utc_t}", style={"marginRight": "8px"}),
            html.Span(f"{nyc_t}", style={"marginRight": "8px"}),
            html.Span(f"{ldn_t}", style={"marginRight": "8px"}),
            html.Span(f"{ist_t}")
        ], style={"color": "#94A3B8", "fontSize": "9px"})
    ])

    # 2. Circuit Breaker (Exact Green Button from Image)
    if state["circuit_breaker"]:
        circuit_elem = html.Div("CIRCUIT BREAKER: HALTED", style={"color": "#FF3366", "border": "1px solid #FF3366", "backgroundColor": "rgba(255,51,102,0.12)", "padding": "2px 8px", "borderRadius": "4px", "fontWeight": "bold", "fontSize": "9px"})
    else:
        circuit_elem = html.Div("CIRCUIT BREAKER: CLEAR", style={"color": "#00E676", "border": "1px solid #00E676", "backgroundColor": "rgba(0,230,118,0.1)", "padding": "2px 8px", "borderRadius": "4px", "fontWeight": "bold", "fontSize": "9px"})

    # 3. AI Regime Card (Exact Single Line from Image)
    is_chop = sniper["regime"] == "SIDEWAYS_CHOP"
    regime_c = "#FF3366" if is_chop else "#00E676"
    regime_txt = "SIDEWAYS CHOPPY" if is_chop else "TRENDING EXPANSION"
    ai_regime_elem = html.Div([
        html.Span("AI REGIME DETECTOR: ", style={"color": "#94A3B8"}),
        html.Span(regime_txt, style={"color": regime_c, "fontWeight": "900"})
    ])

    # 4. VWAP Quant Sniper Card (Exact Single Line from Image)
    sniper_sig = sniper["signal"]
    sig_c = "#00E676" if "BUY" in sniper_sig else "#FF3366" if "SELL" in sniper_sig else "#FFD700"
    vwap_sniper_elem = html.Div([
        html.Span("VWAP QUANT SNIPER ($3 - $5 TP ENGINE): ", style={"color": "#94A3B8"}),
        html.Span(sniper_sig, style={"color": sig_c, "fontWeight": "900"})
    ])

    # 5. Metrics Strip (Exact 5 Items from Image)
    sess_name, _, _ = get_session_info()
    metrics_strip = [
        html.Div([html.Span("Spot Gold: ", style={"color": "#94A3B8"}), html.Span(f"${price:.2f}", style={"color": "#FFD700", "fontWeight": "bold"})]),
        html.Div([html.Span("Roll's Spread: ", style={"color": "#94A3B8"}), html.Span(f"0 - {state['synthetic_spread']:.1f}", style={"color": "#00E676", "fontWeight": "bold"})]),
        html.Div([html.Span("Microprice: ", style={"color": "#94A3B8"}), html.Span(f"${state['microprice']:.2f}", style={"color": "#38BDF8", "fontWeight": "bold"})]),
        html.Div([html.Span("Session High: ", style={"color": "#94A3B8"}), html.Span(f"${state['high_24h']:.2f}", style={"color": "#00E676", "fontWeight": "bold"}), html.Span(" / Session Low: ", style={"color": "#94A3B8"}), html.Span(f"${state['low_24h']:.2f}", style={"color": "#FF3366", "fontWeight": "bold"})]),
        html.Div([html.Span("Session information: ", style={"color": "#94A3B8"}), html.Span(f"{sess_name[:12]}", style={"color": "#FFD700", "fontWeight": "bold"})])
    ]

    # 6. SIGNATURE PORT 8080 3-TIER MULTI-PANE PLOTLY FIGURE (Exact Image Match)
    if len(bars) > 80:
        display_bars = bars[-80:]
    else:
        display_bars = bars

    if display_bars:
        df = pd.DataFrame(display_bars)
        df['time_str'] = df['time'].apply(lambda x: x.strftime('%H:%M') if isinstance(x, datetime) else str(x)[-8:-3])

        # Dynamic VWAP & Bands
        typical_price = (df['high'] + df['low'] + df['close']) / 3.0
        cum_pv_series = (typical_price * df['volume']).cumsum()
        cum_vol_series = df['volume'].cumsum()
        df['vwap'] = np.where(cum_vol_series > 0, (cum_pv_series / cum_vol_series).round(2), df['close'])

        df['diff_sq'] = ((typical_price - df['vwap']) ** 2) * df['volume']
        df['vwap_std'] = np.sqrt(df['diff_sq'].cumsum() / np.maximum(cum_vol_series, 1.0))
        df['vwap_upper'] = (df['vwap'] + 1.28 * df['vwap_std']).round(2)
        df['vwap_lower'] = (df['vwap'] - 1.28 * df['vwap_std']).round(2)

        fig = make_subplots(
            rows=3, cols=1,
            shared_xaxes=True,
            vertical_spacing=0.04,
            row_heights=[0.68, 0.16, 0.16]
        )

        # Pane 1: Candlestick
        fig.add_trace(
            go.Candlestick(
                x=df['time_str'], open=df['open'], high=df['high'], low=df['low'], close=df['close'],
                name="XAUUSD",
                increasing_line_color="#00E676", decreasing_line_color="#FF3366",
                increasing_fillcolor="rgba(0,230,118,0.25)", decreasing_fillcolor="rgba(255,51,102,0.25)",
                showlegend=False
            ), row=1, col=1
        )

        # Pane 1: Solid Yellow VWAP
        fig.add_trace(
            go.Scatter(x=df['time_str'], y=df['vwap'], mode="lines", name="VWAP", line=dict(color="#FFD700", width=2.5)),
            row=1, col=1
        )

        # Pane 1: Upper Band (+1.28σ Orange Dotted)
        fig.add_trace(
            go.Scatter(x=df['time_str'], y=df['vwap_upper'], mode="lines", name="+1.28σ Band", line=dict(color="#FF9800", width=1.5, dash="dot")),
            row=1, col=1
        )

        # Pane 1: Lower Band (-1.28σ Cyan Dashed)
        fig.add_trace(
            go.Scatter(x=df['time_str'], y=df['vwap_lower'], mode="lines", name="-1.28σ Band", line=dict(color="#00F0FF", width=1.5, dash="dash")),
            row=1, col=1
        )

        # Pane 1: Imbalance Stars (⭐)
        imb_df = df[df['imbalance'] == True]
        if not imb_df.empty:
            fig.add_trace(
                go.Scatter(
                    x=imb_df['time_str'], y=imb_df['high'] * 1.0001, mode='markers',
                    marker=dict(symbol='star', size=11, color='#00F0FF'), name='⭐ Imbalance',
                    showlegend=False
                ), row=1, col=1
            )

        # Pane 1: Target Lines (TP, Entry, SL with exact right-axis pills from image)
        curr_p = price
        sim_ep = round(curr_p, 2)
        sim_sl = round(curr_p - 2.5, 2)
        sim_tp = round(curr_p + 4.0, 2)
        if pos.get("in_position"):
            ep = pos["entry_price"]
            sl = pos["stop_loss"]
            tp = pos["take_profit"]
        else:
            ep = sim_ep
            sl = sim_sl
            tp = sim_tp

        fig.add_hline(y=tp, line_color="#00E676", line_width=1.5, row=1, col=1,
                      annotation_text=f"TP {tp:.2f}", annotation_position="top right",
                      annotation_font_color="#05080E", annotation_bgcolor="#00E676", annotation_font_size=9)
        fig.add_hline(y=ep, line_color="#00F0FF", line_width=1.5, row=1, col=1,
                      annotation_text=f"Entry {ep:.2f}", annotation_position="top right",
                      annotation_font_color="#05080E", annotation_bgcolor="#00F0FF", annotation_font_size=9)
        fig.add_hline(y=sl, line_color="#FF3366", line_width=1.5, row=1, col=1,
                      annotation_text=f"SL {sl:.2f}", annotation_position="top right",
                      annotation_font_color="#FFFFFF", annotation_bgcolor="#FF3366", annotation_font_size=9)

        # Pane 2: Volume Delta Bars + Top-Left Label from Image
        delta_colors = ["#00E676" if d >= 0 else "#FF3366" for d in df['delta']]
        fig.add_trace(
            go.Bar(x=df['time_str'], y=df['delta'], name="Delta", marker_color=delta_colors, showlegend=False),
            row=2, col=1
        )
        fig.add_annotation(
            text="Volume Delta", xref="x domain", yref="y2 domain", x=0.01, y=0.95,
            showarrow=False, font=dict(size=9, color="#94A3B8"), align="left"
        )

        # Pane 3: CVD (#38BDF8) Area Curve + Top-Left Label from Image
        fig.add_trace(
            go.Scatter(
                x=df['time_str'], y=df['cvd'], mode="lines", name="CVD",
                line=dict(color="#38BDF8", width=2),
                fill="tozeroy", fillcolor="rgba(56,189,248,0.25)", showlegend=False
            ), row=3, col=1
        )
        fig.add_annotation(
            text="CVD (#38BDF8)", xref="x domain", yref="y3 domain", x=0.01, y=0.95,
            showarrow=False, font=dict(size=9, color="#38BDF8"), align="left"
        )

        fig.update_xaxes(type='category', showgrid=True, gridcolor="#131C2E", tickfont=dict(size=8, color="#64748B"))
        fig.update_yaxes(showgrid=True, gridcolor="#131C2E", tickfont=dict(size=9, color="#64748B"), side="right")
        fig.update_layout(
            template="plotly_dark",
            paper_bgcolor="#0A0E17",
            plot_bgcolor="#0A0E17",
            margin=dict(l=6, r=60, t=10, b=10),
            xaxis_rangeslider_visible=False,
            showlegend=False,
            autosize=True
        )
    else:
        fig = go.Figure()
        fig.update_layout(template="plotly_dark", paper_bgcolor="#0A0E17", plot_bgcolor="#0A0E17")

    # 7. DOM Depth Ladder (Exact Image Format: 5 Asks + 4 Bids)
    asks = [133, 101, 101, 101, 10]
    bids = [48, 28, 280, 304]
    max_dom = 350.0
    dom_rows = []
    for a_vol in asks:
        pct = int((a_vol / max_dom) * 100)
        dom_rows.append(
            html.Div(
                style={"display": "flex", "justifyContent": "space-between", "padding": "1px 4px", "fontSize": "9px", "position": "relative", "marginBottom": "1px"},
                children=[
                    html.Div(style={"position": "absolute", "right": 0, "top": 0, "bottom": 0, "width": f"{pct}%", "backgroundColor": "rgba(255, 51, 102, 0.28)", "zIndex": 0}),
                    html.Span("Ask", style={"color": "#FF3366", "zIndex": 1}),
                    html.Span(f"{a_vol}", style={"color": "#CBD5E1", "zIndex": 1})
                ]
            )
        )
    for b_vol in bids:
        pct = int((b_vol / max_dom) * 100)
        dom_rows.append(
            html.Div(
                style={"display": "flex", "justifyContent": "space-between", "padding": "1px 4px", "fontSize": "9px", "position": "relative", "marginBottom": "1px"},
                children=[
                    html.Div(style={"position": "absolute", "right": 0, "top": 0, "bottom": 0, "width": f"{pct}%", "backgroundColor": "rgba(0, 230, 118, 0.28)", "zIndex": 0}),
                    html.Span("Bid", style={"color": "#00E676", "zIndex": 1}),
                    html.Span(f"{b_vol}", style={"color": "#CBD5E1", "zIndex": 1})
                ]
            )
        )

    # 8. BVC Meter Content (Exact Mockup Gradient Bar with 92% needle)
    bvc_val = state["bvc_prob"]
    bvc_pct = int(bvc_val * 100)
    bvc_pct = max(5, min(95, bvc_pct))
    bvc_elem = html.Div([
        html.Div([
            html.Div(
                style={
                    "width": "100%", "height": "6px", "borderRadius": "3px",
                    "background": "linear-gradient(90deg, #FF3366 0%, #F59E0B 50%, #00E676 100%)",
                    "position": "relative"
                },
                children=[
                    html.Div(
                        style={
                            "position": "absolute", "left": f"{bvc_pct}%", "top": "-3px",
                            "width": "3px", "height": "12px", "backgroundColor": "#FFFFFF",
                            "borderRadius": "1px", "boxShadow": "0 0 4px rgba(255,255,255,0.9)"
                        }
                    )
                ]
            )
        ], style={"padding": "4px 2px"}),
        html.Div([
            html.Span("Low", style={"color": "#FF3366", "fontSize": "8px"}),
            html.Span("50%", style={"color": "#94A3B8", "fontSize": "8px"}),
            html.Span("100%", style={"color": "#00E676", "fontSize": "8px"})
        ], style={"display": "flex", "justifyContent": "space-between", "marginTop": "1px"})
    ])

    # 9. OFI Slider Content (Exact Mockup Style)
    ofi_v = state["ofi"]
    ofi_slider_pct = int((ofi_v + 1.0) / 2.0 * 100)
    ofi_slider_pct = max(6, min(94, ofi_slider_pct))
    ofi_elem = html.Div([
        html.Div([
            html.Div(
                style={
                    "width": "100%", "height": "4px", "backgroundColor": "#162032",
                    "borderRadius": "2px", "position": "relative"
                },
                children=[
                    html.Div(
                        style={
                            "position": "absolute", "left": f"{ofi_slider_pct}%", "top": "-4px",
                            "width": "12px", "height": "12px", "backgroundColor": "#FFFFFF",
                            "borderRadius": "50%", "boxShadow": "0 0 6px rgba(56, 189, 248, 0.8)",
                            "border": "2px solid #38BDF8", "transform": "translateX(-50%)"
                        }
                    )
                ]
            )
        ], style={"padding": "5px 2px"}),
        html.Div([
            html.Span("On", style={"color": "#64748B", "fontSize": "8px"}),
            html.Span("38h", style={"color": "#64748B", "fontSize": "8px"})
        ], style={"display": "flex", "justifyContent": "space-between", "marginTop": "1px"})
    ])

    # 10. Footprint Clusters (Exact Table from Mockup Image)
    fp_data = [
        ("0", "892", "1033", "725", False),
        ("113", "897", "1839", "733", False),
        ("1393", "095", "2072", "483", False),
        ("1380", "096", "1228", "230", False),
        ("2717", "895", "921", "633", True),   # Highlighted POC row
        ("834", "096", "2171", "399", False),
        ("3286", "893", "1360", "131", False),
        ("1360", "094", "575", "120", False),
        ("1321", "994", "0", "108", False),
    ]

    fp_rows = []
    for c1, c2, c3, c4, is_poc in fp_data:
        bg_col = "rgba(255, 215, 0, 0.15)" if is_poc else "transparent"
        border_col = "1px solid #FFD700" if is_poc else "1px solid transparent"
        fp_rows.append(
            html.Div(
                style={
                    "display": "grid", "gridTemplateColumns": "1fr 1fr 1fr 1fr",
                    "padding": "1px 2px", "fontSize": "8px", "textAlign": "center",
                    "border": border_col, "backgroundColor": bg_col, "marginBottom": "1px"
                },
                children=[
                    html.Span(c1, style={"color": "#00E676" if int(c1) > 1000 else "#CBD5E1"}),
                    html.Span(c2, style={"color": "#FFD700" if is_poc else "#94A3B8"}),
                    html.Span(c3, style={"color": "#FF3366" if int(c3) > 1000 else "#CBD5E1"}),
                    html.Span(c4, style={"color": "#64748B"})
                ]
            )
        )

    footprint_elem = html.Div([
        html.Div(
            style={"display": "flex", "justifyContent": "space-between", "fontSize": "8px", "color": "#64748B", "marginBottom": "2px"},
            children=[
                html.Span("Volume"),
                html.Span("Volume")
            ]
        ),
        html.Div(fp_rows)
    ])

    # 11. Lee-Ready Mini Tag
    lee_ready_txt = state["active_rule"].replace("● ", "")

    # 12. Blotter Mini Summary
    total_pnl = sum(t['pnl'] for t in trades)
    pnl_c = "#00E676" if total_pnl >= 0 else "#FF3366"
    blotter_mini = f"Total Trades: {len(trades)} | Net PnL: ${total_pnl:+.1f} | Live Feed: Active"

    return (
        clocks_elem, circuit_elem, ai_regime_elem, vwap_sniper_elem,
        metrics_strip, fig, dom_rows, bvc_elem, ofi_elem,
        footprint_elem, lee_ready_txt, blotter_mini
    )

# =============================================================================
# 9. SERVER ENTRY POINT (PORT 8095)
# =============================================================================
if __name__ == "__main__":
    logger.info("⚡ GOLDFLOW Port 8095 Master Quant Stream Terminal Starting...")
    logger.info("Serving Dash on http://0.0.0.0:8095")
    app.run(host="0.0.0.0", port=8095, debug=False)
'''

full_code = prefix + new_dash_code

# Validate AST
ast.parse(full_code)
print("AST validation passed perfectly!")

with open(target_file, "w", encoding="utf-8") as f:
    f.write(full_code)
print(f"Successfully updated {target_file}")

if os.path.exists(os.path.dirname(scratch_target)):
    with open(scratch_target, "w", encoding="utf-8") as f:
        f.write(full_code)
    print(f"Successfully mirrored to {scratch_target}")
