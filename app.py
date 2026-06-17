import os
import sys
import shutil
import subprocess
import threading
import tempfile
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import re
import ctypes
import platform

if platform.system() == "Windows":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        ctypes.windll.user32.SetProcessDPIAware()

try:
    from svglib.svglib import svg2rlg
    from reportlab.graphics import renderPM
    from PIL import Image, ImageTk
    HAS_SVGLIB = True
except ImportError:
    HAS_SVGLIB = False

# ─────────────────────────────────────────────
#  HELPERS DE LOCALIZAÇÃO DE EXECUTÁVEIS
# ─────────────────────────────────────────────

def find_executable(name):
    path = shutil.which(name)
    if path: return path
    candidates = [
        os.path.expanduser(f"~/.local/bin/{name}"),
        os.path.expanduser(f"~/.cabal/bin/{name}"),
        os.path.expanduser(f"~/.stack/bin/{name}"),
        f"/usr/local/bin/{name}",
    ]
    for c in candidates:
        if os.path.isfile(c) and os.access(c, os.X_OK): return c
    return None

def find_vpype(): return find_executable("vpype")
def find_juicy_gcode(): return find_executable("juicy-gcode")

FLAVOR_TEMPLATE = (
    'gcode-prepend: "G21\\nG90\\nG1 F{feed_rate}\\n"\n'
    'gcode-lift-up: "G0 Z{lift_height}\\n"\n'
    'gcode-lift-down: "G1 Z0 F{plunge_rate}\\n"\n'
)

# ─────────────────────────────────────────────
#  PROCESSAMENTO DE GCODE
# ─────────────────────────────────────────────

def run_vpype(input_svg, output_svg, tolerance="0.2mm", log_callback=None):
    vpype_exe = find_vpype()
    if not vpype_exe:
        vpype_exe = sys.executable
        base_cmd = [vpype_exe, "-m", "vpype"]
    else:
        base_cmd = [vpype_exe]

    in_path = os.path.abspath(input_svg)
    out_path = os.path.abspath(output_svg)

    cmd = base_cmd + [
        "read", in_path,
        "linemerge", "--tolerance", tolerance,
        "linesort",
        "write", out_path,
    ]
    
    if log_callback: log_callback(f"▶ vpype: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        err = result.stderr.strip() if result.stderr else result.stdout.strip()
        raise RuntimeError(f"vpype falhou (Código {result.returncode}):\n{err}")
    if log_callback: log_callback("✓ vpype concluído")

def run_juicy_gcode(input_svg, output_gcode, flavor_file, log_callback=None):
    juicy = find_juicy_gcode()
    if not juicy:
        raise FileNotFoundError("juicy-gcode não encontrado. Verifique as variáveis de ambiente.")

    in_path = os.path.abspath(input_svg)
    out_path = os.path.abspath(output_gcode)
    flav_path = os.path.abspath(flavor_file)

    cmd = [juicy, "-f", flav_path, "-o", out_path, in_path]
    
    if log_callback: log_callback(f"▶ juicy-gcode: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        err = result.stderr.strip() if result.stderr else result.stdout.strip()
        raise RuntimeError(f"juicy-gcode falhou (Código {result.returncode}):\n{err}")
    if log_callback: log_callback("✓ juicy-gcode concluído")

# ─────────────────────────────────────────────
#  PARSER DE G-CODE
# ─────────────────────────────────────────────

def parse_gcode_to_paths(filepath):
    cut_paths = []
    travel_paths = []
    current_cut = []
    x, y = 0.0, 0.0
    
    with open(filepath, 'r') as f:
        for line in f:
            line = line.split(';')[0].strip()
            if not line: continue
            
            cmd_match = re.match(r'^(G00|G01|G0|G1)\b', line, re.IGNORECASE)
            if not cmd_match: continue
            
            cmd = cmd_match.group(1).upper()
            x_match = re.search(r'X\s*(-?\d+\.?\d*)', line, re.IGNORECASE)
            y_match = re.search(r'Y\s*(-?\d+\.?\d*)', line, re.IGNORECASE)
            
            new_x = float(x_match.group(1)) if x_match else x
            new_y = float(y_match.group(1)) if y_match else y
            
            if cmd in ['G00', 'G0']:
                travel_paths.append([(x, y), (new_x, new_y)])
                if current_cut:
                    cut_paths.append(current_cut)
                    current_cut = []
            elif cmd in ['G01', 'G1']:
                if not current_cut:
                    current_cut.append((x, y))
                current_cut.append((new_x, new_y))
                
            x, y = new_x, new_y
            
    if current_cut:
        cut_paths.append(current_cut)

    all_pts = [pt for pl in (cut_paths + travel_paths) for pt in pl]
    if all_pts:
        xs = [p[0] for p in all_pts]
        ys = [p[1] for p in all_pts]
        bounds = (min(xs), min(ys), max(xs)-min(xs), max(ys)-min(ys))
    else:
        bounds = (0, 0, 100, 100)
    return cut_paths, travel_paths, bounds

def gradient_color(t):
    # Restaurado o contraste perfeito original (Verde neon brilhante para Azul elétrico)
    r = int(0x00 + t * (0x29 - 0x00))
    g = int(0xe6 + t * (0x79 - 0xe6))
    b = int(0x76 + t * (0xff - 0x76))
    return f"#{r:02x}{g:02x}{b:02x}"

# ─────────────────────────────────────────────
#  APLICAÇÃO PRINCIPAL
# ─────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("MonteBot GCreator — SVG to G-code")
        self.configure(bg="#0d0f14")
        self.state("zoomed")
        
        # Estados da UI e Cache
        self.svg_path = None
        self.temp_gcode_path = None
        self.cut_paths = []
        self.travel_paths = []
        self.gcode_bounds = (0, 0, 100, 100)
        self.preview_mode = "none"
        self.tk_img = None
        self.svg_pil_base = None  # Cache crucial para eliminar o lag de renderização do SVG
        self._processing = False

        # Controles de Navegação (Zoom Virtual & Pan Físico)
        self.zoom_level = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        self.drag_start_x = 0
        self.drag_start_y = 0

        self._build_ui()
        self._check_deps()
        self._bind_navigation_events()

    def _build_ui(self):
        self.columnconfigure(0, weight=0)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        # ── Painel esquerdo
        left = tk.Frame(self, bg="#12151c", width=340)
        left.grid(row=0, column=0, sticky="nsew")
        left.grid_propagate(False)
        left.columnconfigure(0, weight=1)

        hdr = tk.Frame(left, bg="#12151c")
        hdr.grid(row=0, column=0, sticky="ew", padx=20, pady=(24, 4))
        tk.Label(hdr, text="MONTEBOT GCREATOR", font=("Courier New", 15, "bold"), fg="#00e676", bg="#12151c").pack(anchor="w")
        tk.Label(hdr, text="v0.0.1 • ufu monte carmelo", font=("Courier New", 8), fg="#4a5568", bg="#12151c").pack(anchor="w")

        sep = tk.Frame(left, bg="#1e2330", height=1)
        sep.grid(row=1, column=0, sticky="ew", padx=16, pady=8)

        # Entrada
        btn_frame = tk.Frame(left, bg="#12151c")
        btn_frame.grid(row=2, column=0, sticky="ew", padx=16, pady=4)
        self.btn_svg = self._make_btn(btn_frame, "＋ Selecionar SVG", self._open_svg, accent=True)
        self.btn_svg.pack(fill="x")
        self.lbl_file = tk.Label(left, text="nenhum arquivo", font=("Courier New", 8), fg="#4a5568", bg="#12151c", wraplength=300, justify="left")
        self.lbl_file.grid(row=3, column=0, sticky="w", padx=20, pady=(0, 8))

        sep2 = tk.Frame(left, bg="#1e2330", height=1)
        sep2.grid(row=4, column=0, sticky="ew", padx=16, pady=10)

        # Parâmetros
        tk.Label(left, text="PARÂMETROS", font=("Courier New", 8, "bold"), fg="#4a5568", bg="#12151c").grid(row=5, column=0, sticky="w", padx=20, pady=(0, 2))
        params = tk.Frame(left, bg="#12151c")
        params.grid(row=6, column=0, sticky="ew", padx=16, pady=4)
        params.columnconfigure(1, weight=1)

        self._param_entries = {}
        fields = [
            ("Feed rate (mm/min)", "feed_rate", "1000"),
            ("Plunge rate (mm/min)", "plunge_rate", "600"),
            ("Altura lift (mm)", "lift_height", "5"),
            ("Tolerância vpype", "tolerance", "0.2mm"),
        ]
        for i, (label, key, default) in enumerate(fields):
            tk.Label(params, text=label, font=("Courier New", 8), fg="#8892a4", bg="#12151c", anchor="w").grid(row=i*2, column=0, columnspan=2, sticky="w", pady=(6, 0))
            e = tk.Entry(params, font=("Courier New", 10), bg="#1a1f2e", fg="#e2e8f0", insertbackground="#00e676", relief="flat", bd=0, highlightthickness=1, highlightcolor="#00e676", highlightbackground="#2d3748")
            e.insert(0, default)
            e.grid(row=i*2+1, column=0, columnspan=2, sticky="ew", pady=(1, 0), ipady=5, padx=1)
            self._param_entries[key] = e

        sep3 = tk.Frame(left, bg="#1e2330", height=1)
        sep3.grid(row=7, column=0, sticky="ew", padx=16, pady=12)

        # Ações
        btn_frame2 = tk.Frame(left, bg="#12151c")
        btn_frame2.grid(row=8, column=0, sticky="ew", padx=16, pady=4)
        self.btn_gen = self._make_btn(btn_frame2, "⚙ Gerar G-code", self._generate, accent=False)
        self.btn_gen.pack(fill="x", pady=(0, 6))
        self.btn_gen.configure(state="disabled")
        
        self.btn_save = tk.Button(btn_frame2, text="💾 Salvar G-code", command=self._save_gcode, font=("Courier New", 9, "bold"), bg="#1a1f2e", fg="#8892a4", activebackground="#2d3748", activeforeground="#8892a4", relief="flat", bd=0, pady=8, cursor="hand2")
        self.btn_save.pack(fill="x")
        self.btn_save.configure(state="disabled")

        self.lbl_status = tk.Label(left, text="", font=("Courier New", 8), fg="#00e676", bg="#12151c", wraplength=260, justify="left")
        self.lbl_status.grid(row=9, column=0, sticky="w", padx=20, pady=4)

        # Log
        tk.Label(left, text="LOG", font=("Courier New", 8, "bold"), fg="#4a5568", bg="#12151c").grid(row=10, column=0, sticky="w", padx=20, pady=(10,0))
        log_frame = tk.Frame(left, bg="#0a0c12")
        log_frame.grid(row=11, column=0, sticky="nsew", padx=12, pady=(4, 12))
        left.rowconfigure(11, weight=1)

        self.log_text = tk.Text(log_frame, font=("Courier New", 7), bg="#0a0c12", fg="#4a5568", relief="flat", bd=0, state="disabled", wrap="word", height=6)
        scrollbar = tk.Scrollbar(log_frame, command=self.log_text.yview, bg="#12151c", troughcolor="#0a0c12", activebackground="#2d3748")
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True, padx=4, pady=4)
        scrollbar.pack(side="right", fill="y")

        # ── Painel direito
        right = tk.Frame(self, bg="#0d0f14")
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)

        preview_hdr = tk.Frame(right, bg="#12151c", height=40)
        preview_hdr.grid(row=0, column=0, sticky="ew")
        preview_hdr.columnconfigure(1, weight=1)
        
        self.lbl_preview_title = tk.Label(preview_hdr, text="PREVIEW", font=("Courier New", 9, "bold"), fg="#8892a4", bg="#12151c")
        self.lbl_preview_title.grid(row=0, column=0, padx=16, pady=10, sticky="w")

        self.lbl_nav_hint = tk.Label(preview_hdr, text="[Scroll: Zoom | Botão Esquerdo Pressionado: Arrastar Folha]", font=("Courier New", 8), fg="#4a5568", bg="#12151c")
        self.lbl_nav_hint.grid(row=0, column=1, padx=10, sticky="w")
        self.lbl_nav_hint.grid_remove()

        self.legend_frame = tk.Frame(preview_hdr, bg="#12151c")
        self.legend_frame.grid(row=0, column=2, padx=16, pady=6, sticky="e")
        
        tk.Label(self.legend_frame, text="transição", font=("Courier New", 7), fg="#ffd600", bg="#12151c").pack(side="left")
        tc = tk.Canvas(self.legend_frame, width=20, height=10, bg="#12151c", highlightthickness=0)
        tc.pack(side="left", padx=(2, 8))
        tc.create_line(0, 5, 20, 5, fill="#ffd600", dash=(2, 2))
        
        tk.Label(self.legend_frame, text="início", font=("Courier New", 7), fg="#00e676", bg="#12151c").pack(side="left")
        grad_canvas = tk.Canvas(self.legend_frame, width=60, height=10, bg="#12151c", highlightthickness=0)
        grad_canvas.pack(side="left", padx=4)
        for i in range(60):
            grad_canvas.create_line(i, 0, i, 10, fill=gradient_color(i / 59))
        tk.Label(self.legend_frame, text="fim", font=("Courier New", 7), fg="#2979ff", bg="#12151c").pack(side="left")
        self.legend_frame.grid_remove()

        self.canvas = tk.Canvas(right, bg="#0d0f14", highlightthickness=0, cursor="fleur")
        self.canvas.grid(row=1, column=0, sticky="nsew", padx=16, pady=16)
        self.canvas.bind("<Configure>", self._on_canvas_resize)

        self._draw_placeholder("Adicione um SVG para visualizar.")

    def _make_btn(self, parent, text, cmd, accent=False):
        bg, fg, abg = ("#00c853", "#0d0f14", "#00e676") if accent else ("#1a1f2e", "#8892a4", "#2d3748")
        return tk.Button(parent, text=text, command=cmd, font=("Courier New", 9, "bold"), bg=bg, fg=fg, activebackground=abg, activeforeground=fg, relief="flat", bd=0, pady=10, padx=12, cursor="hand2")

    def _draw_placeholder(self, text):
        self.canvas.delete("all")
        w, h = self.canvas.winfo_width() or 600, self.canvas.winfo_height() or 400
        self.canvas.create_text(w//2, h//2, text=text, font=("Courier New", 11), fill="#2d3748", justify="center")
        for x in range(0, w, 40): self.canvas.create_line(x, 0, x, h, fill="#111520", width=1)
        for y in range(0, h, 40): self.canvas.create_line(0, y, w, y, fill="#111520", width=1)

    def _on_canvas_resize(self, event):
        self._update_view()

    def _check_deps(self):
        if not HAS_SVGLIB:
            self._log("⚠ 'svglib'/'pillow' não encontradas. O preview do SVG puro será desativado.")

    # ── EVENTOS DE NAVEGAÇÃO INTERATIVA (SEM LAG) ──────

    def _bind_navigation_events(self):
        # Arrastar (Pan)
        self.canvas.bind("<ButtonPress-1>", self._start_pan)
        self.canvas.bind("<B1-Motion>", self._execute_pan)
        
        # Zoom (Scroll Wheel)
        self.canvas.bind("<MouseWheel>", self._on_zoom)
        self.canvas.bind("<Button-4>", self._on_zoom) # Linux Up
        self.canvas.bind("<Button-5>", self._on_zoom) # Linux Down

    def _start_pan(self, event):
        self.drag_start_x = event.x
        self.drag_start_y = event.y

    def _execute_pan(self, event):
        dx = event.x - self.drag_start_x
        dy = event.y - self.drag_start_y
        
        # Incrementa o rastreamento virtual do deslocamento
        self.pan_x += dx
        self.pan_y += dy
        self.drag_start_x = event.x
        self.drag_start_y = event.y
        
        # SOLUÇÃO DE OURO: Move todos os vetores fisicamente no hardware do canvas sem redesenhar por código.
        # Tempo de execução: 0ms. Lag eliminado completamente!
        self.canvas.move("all", dx, dy)

    def _on_zoom(self, event):
        if event.num == 4 or event.delta > 0:
            factor = 1.15
        elif event.num == 5 or event.delta < 0:
            factor = 0.85
        else:
            factor = 1.0

        new_zoom = self.zoom_level * factor
        if 0.1 <= new_zoom <= 50.0:
            # Reajusta o pan matemático para aproximar em direção ao ponteiro do mouse
            cx, cy = event.x, event.y
            self.pan_x = cx - (cx - self.pan_x) * factor
            self.pan_y = cy - (cy - self.pan_y) * factor
            self.zoom_level = new_zoom
            
            # Atualiza o desenho por inteiro apenas nas etapas discretas do scroll
            self._update_view()

    def _reset_navigation(self):
        self.zoom_level = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0

    def _update_view(self):
        if self.preview_mode == "pure_svg":
            self._render_pure_svg()
        elif self.preview_mode == "gcode":
            self._render_gcode_preview()
        else:
            self._draw_placeholder("Adicione um SVG para visualizar.")

    # ── MÉTODOS DE RENDERIZAÇÃO REESTRUTURADOS ─────────

    def _open_svg(self):
        path = filedialog.askopenfilename(title="Selecionar SVG", filetypes=[("SVG files", "*.svg")])
        if not path: return
        self.svg_path = path
        short = os.path.basename(path)
        self.lbl_file.configure(text=short, fg="#e2e8f0")
        
        self.btn_gen.configure(state="normal", bg="#2979ff", fg="#ffffff", activebackground="#448aff")
        self.btn_save.configure(state="disabled")
        self.lbl_status.configure(text="Configuração pronta para gerar.")
        self.legend_frame.grid_remove()
        self.lbl_nav_hint.grid()
        self._reset_navigation()

        # CACHE DO SVG EM MEMÓRIA: Evita re-ler o arquivo do disco no loop de renderização
        self.svg_pil_base = None
        if HAS_SVGLIB:
            try:
                import io
                drawing = svg2rlg(self.svg_path)
                if drawing:
                    img_data = io.BytesIO()
                    renderPM.drawToFile(drawing, img_data, fmt="PNG")
                    img_data.seek(0)
                    self.svg_pil_base = Image.open(img_data).convert("RGBA")
            except Exception as e:
                self._log(f"Erro ao criar cache de imagem do SVG: {e}")

        if self.svg_pil_base:
            self.preview_mode = "pure_svg"
            self.lbl_preview_title.config(text="PREVIEW — Imagem Original (SVG)")
            self._render_pure_svg()
        else:
            self.preview_mode = "none"
            self._draw_placeholder("SVG carregado.\nClique em 'Gerar' para visualizar a execução.")
            
        self._log(f"SVG carregado: {short}")

    def _get_params(self):
        return {k: e.get() for k, e in self._param_entries.items()}

    def _generate(self):
        if self._processing or not self.svg_path: return
        self._processing = True
        self.btn_gen.configure(state="disabled", text="⏳ Processando…")
        self.btn_save.configure(state="disabled")
        self.lbl_status.configure(text="Gerando temporariamente...", fg="#8892a4")
        
        t = threading.Thread(target=self._do_generate, args=(self._get_params(),), daemon=True)
        t.start()

    def _do_generate(self, params):
        tmpdir = tempfile.mkdtemp(prefix="svggcode_")
        try:
            optimized_svg = os.path.join(tmpdir, "optimized.svg")
            flavor_file   = os.path.join(tmpdir, "flavor.yaml")
            fd, temp_gcode = tempfile.mkstemp(suffix=".gcode", prefix="gerado_")
            os.close(fd)

            with open(flavor_file, "w") as f:
                f.write(FLAVOR_TEMPLATE.format(**params))

            self._ui_log("Otimizando com vpype…")
            run_vpype(self.svg_path, optimized_svg, tolerance=params["tolerance"], log_callback=self._ui_log)

            self._ui_log("Gerando G-code...")
            run_juicy_gcode(optimized_svg, temp_gcode, flavor_file, log_callback=self._ui_log)

            with open(temp_gcode, 'r') as f:
                lines = f.readlines()

            header = ["G21\n", "G90\n", f"G1 F{params['feed_rate']}\n"]
            if not any("G21" in line for line in lines[:5]):
                lines = header + lines
                
            with open(temp_gcode, 'w') as f:
                f.writelines(lines)

            self._ui_log("Analisando movimentos...")
            cut_p, trav_p, bounds = parse_gcode_to_paths(temp_gcode)
            
            self.temp_gcode_path = temp_gcode
            self.cut_paths = cut_p
            self.travel_paths = trav_p
            self.gcode_bounds = bounds

            with open(temp_gcode, 'r') as f:
                lines = f.readlines()

            CUT_DEPTH = -1.0

            new_lines = []
            for line in lines:
                line_strip = line.strip()
                if "Z" in line_strip:
                    new_lines.append(line)
                    continue
                    
                parts = line_strip.split()
                if not parts:
                    new_lines.append(line)
                    continue
                    
                cmd = parts[0]
                if cmd in ["G0", "G00"]:
                    new_lines.append(f"{line_strip} Z{params['lift_height']}\n")
                elif cmd in ["G1", "G01"]:
                    new_lines.append(f"{line_strip} Z{CUT_DEPTH}\n")
                else:
                    new_lines.append(line)

            with open(temp_gcode, 'w') as f:
                f.writelines(new_lines)

            self.after(0, self._on_generate_done, True, "G-code pronto com cabeçalho corrigido!")

        except Exception as e:
            self.after(0, self._on_generate_done, False, str(e))

    def _on_generate_done(self, success, msg):
        self._processing = False
        self.btn_gen.configure(state="normal", text="⚙ Gerar Novamente", bg="#1a1f2e", fg="#8892a4")
        if success:
            self.lbl_status.configure(text=msg, fg="#00e676")
            self.btn_save.configure(state="normal", bg="#00c853", fg="#0d0f14")
            self.preview_mode = "gcode"
            self.lbl_preview_title.config(text="PREVIEW — Leitura do G-code (Cortes + Transições)")
            self.legend_frame.grid()
            self.lbl_nav_hint.grid()
            self._reset_navigation() 
            self._render_gcode_preview()
            self._log("✓ Visualização atualizada.")
        else:
            self.lbl_status.configure(text="Erro! Ver log.", fg="#ff5252")
            messagebox.showerror("Erro ao gerar G-code", msg)
            self._log("✗ " + msg)

    def _save_gcode(self):
        if not self.temp_gcode_path or not os.path.exists(self.temp_gcode_path):
            messagebox.showwarning("Aviso", "Nenhum G-code na memória. Gere primeiro.")
            return
            
        default_name = os.path.splitext(os.path.basename(self.svg_path))[0] + "_otimizado.gcode"
        dest = filedialog.asksaveasfilename(
            title="Salvar G-code como",
            initialfile=default_name,
            defaultextension=".gcode",
            filetypes=[("G-code", "*.gcode *.nc *.cnc"), ("Todos os arquivos", "*.*")]
        )
        if dest:
            try:
                shutil.copy(self.temp_gcode_path, dest)
                self.lbl_status.configure(text="Arquivo salvo com sucesso!", fg="#00e676")
                self._log(f"Salvo em: {os.path.basename(dest)}")
                messagebox.showinfo("Sucesso", "Arquivo salvo perfeitamente!")
            except Exception as e:
                messagebox.showerror("Erro ao salvar", str(e))

    # ── RE-RENDERIZAÇÃO RÁPIDA (CHAMADA APENAS NO ZOOM) ──

    def _render_pure_svg(self):
        if not self.svg_pil_base: return
        self.canvas.delete("all")
        cw, ch = self.canvas.winfo_width() or 600, self.canvas.winfo_height() or 400

        # Grid dinâmico baseado na folha
        grid_size = int(40 * self.zoom_level)
        if grid_size > 5:
            start_x = int(self.pan_x) % grid_size
            start_y = int(self.pan_y) % grid_size
            for x in range(start_x, cw, grid_size): self.canvas.create_line(x, 0, x, ch, fill="#111520", width=1)
            for y in range(start_y, ch, grid_size): self.canvas.create_line(0, y, cw, y, fill="#111520", width=1)

        try:
            # Usa a imagem do cache e faz o resize super veloz
            img = self.svg_pil_base
            img_w, img_h = img.size
            base_scale = min((cw - 80) / img_w, (ch - 80) / img_h)
            
            final_w = max(10, int(img_w * base_scale * self.zoom_level))
            final_h = max(10, int(img_h * base_scale * self.zoom_level))
            
            resample_filter = getattr(Image, "Resampling", Image).LANCZOS
            img_resized = img.resize((final_w, final_h), resample_filter)

            self.tk_img = ImageTk.PhotoImage(img_resized)
            
            pos_x = (cw // 2) + self.pan_x
            pos_y = (ch // 2) + self.pan_y
            self.canvas.create_image(pos_x, pos_y, image=self.tk_img, anchor="center")
            
        except Exception as e:
            self._log(f"Erro ao renderizar imagem pura: {e}")
            self._draw_placeholder("Erro ao mostrar SVG.")

    def _render_gcode_preview(self):
        self.canvas.delete("all")
        if not self.cut_paths and not self.travel_paths:
            self._draw_placeholder("Nenhuma rota encontrada no G-code.")
            return

        cw, ch = self.canvas.winfo_width() or 600, self.canvas.winfo_height() or 400
        
        # Grid ajustável
        grid_size = int(40 * self.zoom_level)
        if grid_size > 5:
            start_x = int(self.pan_x) % grid_size
            start_y = int(self.pan_y) % grid_size
            for x in range(start_x, cw, grid_size): self.canvas.create_line(x, 0, x, ch, fill="#111520", width=1)
            for y in range(start_y, ch, grid_size): self.canvas.create_line(0, y, cw, y, fill="#111520", width=1)

        vx, vy, vw, vh = self.gcode_bounds
        if vw == 0 or vh == 0: return

        pad = 40
        base_scale = min((cw - pad*2) / vw, (ch - pad*2) / vh)
        scale = base_scale * self.zoom_level
        
        ox = pad + (cw - pad*2 - vw * scale) / 2 + self.pan_x
        oy = pad + (ch - pad*2 - vh * scale) / 2 + self.pan_y

        def to_canvas(x, y):
            cx = ox + (x - vx) * scale
            cy = oy + (vh * scale) - ((y - vy) * scale)
            return cx, cy

        # Desenhar movimentos de Transição (G0)
        for t_path in self.travel_paths:
            pt1 = to_canvas(*t_path[0])
            pt2 = to_canvas(*t_path[1])
            self.canvas.create_line(pt1[0], pt1[1], pt2[0], pt2[1], fill="#ffd600", dash=(2, 4), width=1)

        # Desenhar movimentos de Corte (G1) com gradiente restaurado de alto contraste
        total_segs = sum(max(0, len(pl) - 1) for pl in self.cut_paths)
        seg_idx = 0
        for pl in self.cut_paths:
            pts_c = [to_canvas(px, py) for px, py in pl]
            for i in range(len(pts_c) - 1):
                t = seg_idx / max(1, total_segs - 1) if total_segs > 1 else 1.0
                line_w = max(1, int(1 * self.zoom_level * 0.5))
                line_w = min(line_w, 4)
                
                self.canvas.create_line(pts_c[i][0], pts_c[i][1], pts_c[i+1][0], pts_c[i+1][1],
                                       fill=gradient_color(t), width=line_w, capstyle="round")
                seg_idx += 1

        # Indicadores
        if self.cut_paths and self.cut_paths[0]:
            sx, sy = to_canvas(*self.cut_paths[0][0])
            self.canvas.create_oval(sx-5, sy-5, sx+5, sy+5, fill="#00e676", outline="#0d0f14", width=2)
            self.canvas.create_text(sx+10, sy-10, text="início", font=("Courier New", 8, "bold"), fill="#00e676", anchor="w")

        if self.cut_paths and self.cut_paths[-1]:
            ex, ey = to_canvas(*self.cut_paths[-1][-1])
            self.canvas.create_oval(ex-5, ey-5, ex+5, ey+5, fill="#2979ff", outline="#0d0f14", width=2)
            self.canvas.create_text(ex+10, ey+10, text="fim", font=("Courier New", 8, "bold"), fill="#2979ff", anchor="w")

    # ── LOG ─────────────────────────────────────

    def _ui_log(self, msg):
        self.after(0, self._log, msg)

    def _log(self, msg):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

if __name__ == "__main__":
    app = App()
    app.mainloop()