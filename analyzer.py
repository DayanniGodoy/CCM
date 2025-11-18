# analyzer.py
from __future__ import annotations

import time, re
from typing import Tuple, Optional

# Export explícito para evitar sorpresas con imports desde la GUI
__all__ = [
    "TrafficDetector",
    "detectar_con_driver",
    "detect_all_segments",
    "capture_and_save",
    "shutdown_detectors",
]

# ===================== Estado global =====================
_LAST_DRIVER = None          # driver Selenium más reciente
_GLOBAL_DETECTORS = set()    # detectores vivos (para cerrarlos desde GUI)
_DETECTOR_SINGLETON = None   # se reutiliza entre llamadas GUI


def shutdown_detectors():
    """
    Cierra todos los detectores vivos (y sus Chrome/Drivers).
    Idempotente y segura.
    """
    for det in list(_GLOBAL_DETECTORS):
        try:
            det.close()
        except Exception:
            pass


# ===================== Selenium (import diferido) =====================
def _get_selenium_bits():
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.common.exceptions import TimeoutException, JavascriptException
        return webdriver, Options, By, WebDriverWait, EC, TimeoutException, JavascriptException
    except Exception as e:
        raise RuntimeError(
            "No se pudo importar Selenium. Instala en el venv: pip install selenium"
        ) from e


# ===================== Driver =====================
def _build_driver(cfg):
    webdriver, Options, *_ = _get_selenium_bits()
    opts = Options()
    opts.add_argument("--start-maximized")

    # Perfil persistente opcional
    if getattr(cfg, "perfil_persistente", False):
        try:
            from config import PROFILE_DIR  # import en runtime
            from pathlib import Path
            opts.add_argument(f"--user-data-dir={str(Path(PROFILE_DIR))}")
        except Exception:
            pass

    if getattr(cfg, "headless", False):
        opts.add_argument("--headless=new")
        opts.add_argument("--disable-gpu")

    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--no-sandbox")
    return webdriver.Chrome(options=opts)


def _esperar_panel(driver, timeout=30):
    _, _, By, WebDriverWait, EC, TimeoutException, _ = _get_selenium_bits()
    WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.XPATH, "//app-traffic-view-list-container"))
    )


def _activar_traffic_view(driver, log):
    _, _, By, _, _, TimeoutException, _ = _get_selenium_bits()
    log("⏳ Buscando chip 'Traffic View'…")
    for _ in range(60):
        try:
            chips = driver.find_elements(By.TAG_NAME, "wz-checkable-chip")
            for chip in chips:
                try:
                    contenido = chip.find_element(By.CLASS_NAME, "chip-content").text.strip()
                except Exception:
                    continue
                if any(k in contenido for k in ("Traffic", "Vista de tráfico", "Traffic View")):
                    try:
                        chip.click()
                    except Exception:
                        pass
                    log("✅ Chip 'Traffic View' activado.")
                    try:
                        _esperar_panel(driver, timeout=30)
                    except TimeoutException:
                        pass
                    return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError("No se encontró el botón de Traffic View en 60 s")


# ===================== Scroll & extracción =====================
def _js_get_scrollable_container(driver):
    # Evita “return ( ... );” y maneja fallback
    script = r"""
return (function(){
  const first = document.querySelector('app-traffic-view-route');
  function getScrollable(el){
    let n = el;
    while (n && !(n.scrollHeight > n.clientHeight)) n = n.parentElement;
    return n || document.scrollingElement;
  }
  return first ? getScrollable(first) : document.scrollingElement;
})();
"""
    try:
        return driver.execute_script(script)
    except Exception:
        try:
            return driver.execute_script("return document.scrollingElement;")
        except Exception:
            return None


def _cargar_lista_completa(driver, log, max_scrolls=400, pause=0.18):
    _, _, By, WebDriverWait, EC, _, _ = _get_selenium_bits()
    WebDriverWait(driver, 20).until(
        EC.visibility_of_element_located((By.CSS_SELECTOR, "app-traffic-view-route"))
    )

    def contar():
        return driver.execute_script("return document.querySelectorAll('app-traffic-view-route').length;")

    last_count = -1
    same = 0
    scrollable = _js_get_scrollable_container(driver)
    if scrollable is None:
        scroll_cmd = "window.scrollBy(0, 600);"
        height_cmd = "return document.scrollingElement.scrollHeight;"
        get_top_cmd = "return document.scrollingElement.scrollTop;"
        set_top_cmd = "document.scrollingElement.scrollTop = arguments[0];"
    else:
        scroll_cmd = "arguments[0].scrollBy(0, 600);"
        height_cmd = "return arguments[0].scrollHeight;"
        get_top_cmd = "return arguments[0].scrollTop;"
        set_top_cmd = "arguments[0].scrollTop = arguments[1];"

    prev_h = driver.execute_script(height_cmd, scrollable) if scrollable else driver.execute_script(height_cmd)
    for _ in range(max_scrolls):
        if scrollable:
            driver.execute_script(scroll_cmd, scrollable)
        else:
            driver.execute_script(scroll_cmd)
        time.sleep(pause)
        new_h = driver.execute_script(height_cmd, scrollable) if scrollable else driver.execute_script(height_cmd)
        if new_h <= prev_h:
            top = driver.execute_script(get_top_cmd, scrollable) if scrollable else driver.execute_script(get_top_cmd)
            if scrollable:
                driver.execute_script(set_top_cmd, scrollable, max(0, top - 50))
            else:
                driver.execute_script(set_top_cmd, max(0, top - 50))
            time.sleep(0.05)
        prev_h = new_h
        count = contar()
        if count == last_count:
            same += 1
        else:
            same = 0
        last_count = count
        if same >= 2:
            break

    # Sacudida final
    if scrollable:
        driver.execute_script("arguments[0].scrollTop = Math.max(0, arguments[0].scrollTop - 200);", scrollable)
        time.sleep(0.07)
        driver.execute_script("arguments[0].scrollTop = arguments[0].scrollHeight;", scrollable)
    else:
        driver.execute_script("document.scrollingElement.scrollTop = Math.max(0, document.scrollingElement.scrollTop - 200);")
        time.sleep(0.07)
        driver.execute_script("document.scrollingElement.scrollTop = document.scrollingElement.scrollHeight;")
    time.sleep(0.15)

def _js_extraer_tramos(driver):
    """
    Extracción principal. Además de textos, determina la sección del panel
    (Unusual/Watchlist) y la embebe como `section_flag` ('unusual' | 'watch' | '').
    Regla pedida:
      - WATCHLIST  = tarjeta con menú de acciones (route-menu con Edit/Delete)
      - INUSUAL    = SIN menú de acciones PERO con bloque de stats (tarjeta completa)
      - (Ignoramos jam-level y 'as usual/longer than usual')
    """
    script = r"""
// ======== Helpers que atraviesan Shadow DOM ========
function qDeep(el, sel){
  if (!el) return null;
  let n = null;
  try { n = el.querySelector ? el.querySelector(sel) : null; } catch(e){}
  if (n) return n;
  if (el.shadowRoot){
    try { n = el.shadowRoot.querySelector(sel); } catch(e){}
  }
  return n || null;
}
function qTextDeep(el, sel){
  const n = qDeep(el, sel);
  if (!n) return '';
  const t = (n.innerText || n.textContent || '').trim();
  return t;
}
function getJamDeep(el){
  const cand = [el];
  if (el && el.shadowRoot) cand.push(el.shadowRoot);
  for (const c of cand){
    try{
      const n = c.querySelector ? c.querySelector('.jam-level') : null;
      if (n){
        const m = (n.className || '').match(/jam-level-(\d)/);
        if (m) return parseInt(m[1],10);
      }
    }catch(e){}
  }
  return null;
}
// Sube por parentElement y, si entra a un ShadowRoot, cruza con host
function climbToSection(el){
  let n = el;
  let guard = 0;
  while (n && guard++ < 80){
    if (n.closest){
      const sec = n.closest('app-traffic-view-sidebar-section');
      if (sec) return sec;
    }
    const root = n.getRootNode ? n.getRootNode() : null;
    if (root && root.host){
      n = root.host;
      continue;
    }
    n = n.parentElement;
  }
  return null;
}
function sectionFlagFor(el){
  const sec = climbToSection(el);
  if (!sec) return '';
  let cap = qDeep(sec, ':scope > wz-caption') || qDeep(sec, 'wz-caption');
  let t = (cap && (cap.innerText || cap.textContent) || '').trim().toLowerCase();
  if (/unusual|inusual/.test(t)) return 'unusual';
  if (/watchlist|lista de seguimiento/.test(t)) return 'watch';
  cap = qDeep(sec, '.section-header') || qDeep(sec, '[class*=header]');
  if (cap){
    t = (cap.innerText || cap.textContent || '').trim().toLowerCase();
    if (/unusual|inusual/.test(t)) return 'unusual';
    if (/watchlist|lista de seguimiento/.test(t)) return 'watch';
  }
  return '';
}

// ======== Helpers específicos para esta regla ========
// 1) ¿Tiene menú de acciones (Edit/Delete)? -> WATCHLIST
function hasRouteMenu(el){
  // buscamos cualquier rastro de <div class="route-menu"> y wz-menu/wz-menu-item dentro del mismo route
  const variants = [
    '.route-menu wz-menu-item',
    '.route-menu wz-menu',
    '.route-menu',
    'wz-menu-item',
    'wz-menu'
  ];
  for (const sel of variants){
    const n = qDeep(el, sel);
    if (n) return true;
  }
  return false;
}

// 2) ¿Tiene bloque de stats “normal”? -> nos sirve para certificar INUSUAL
function hasStats(el){
  // basta con que exista el contenedor de stats, y al menos uno de current/historic
  const statsHost = qDeep(el, 'app-traffic-view-route-stats');
  if (!statsHost) return false;
  const hasCurr = !!qDeep(el, '.current-stat');
  const hasHist = !!qDeep(el, '.historic-stat');
  return hasCurr || hasHist;
}

// ======== Extracción principal ========
let routes = Array.from(document.querySelectorAll('app-traffic-view-route'));
if (routes.length === 0){
  const secs = Array.from(document.querySelectorAll('app-traffic-view-sidebar-section'));
  for (const sec of secs){
    if (sec) routes = routes.concat(Array.from(sec.querySelectorAll('app-traffic-view-route')));
    if (sec && sec.shadowRoot){
      try{ routes = routes.concat(Array.from(sec.shadowRoot.querySelectorAll('app-traffic-view-route'))); }catch(e){}
    }
  }
}

const out = [];
for (const r of routes){
  const name = qTextDeep(r, 'wz-subhead4');
  if (!name) continue;

  const current = qTextDeep(r, '.current-stat');
  const historic = qTextDeep(r, '.historic-stat');
  const dist = qTextDeep(r, '.route-distance');
  const jam = getJamDeep(r);

  // --- Señales de clasificación
  const flag_from_section = sectionFlagFor(r); // puede venir vacío
  const menuPresent = hasRouteMenu(r);
  const statsPresent = hasStats(r);

  // *** PRIORIDAD DE BANDERA ***
  // 1) Si hay menú -> WATCHLIST (no tocar lo que ya funciona)
  // 2) Si NO hay menú pero hay stats -> INUSUAL
  // (Ignoramos jam level y textos as/longer than usual)
  let flag = '';
  if (menuPresent){
    flag = 'watch';
  } else if (statsPresent){
    flag = 'unusual';
  } else {
    // si quisieras, podrías dejar caer a lo que diga la sección
    // pero por la regla pedida, nos quedamos así en '' (desconocido)
    if (flag_from_section === 'unusual' || flag_from_section === 'watch'){
      // opcionalmente podrías usarlo de respaldo:
      // flag = flag_from_section;
    }
  }

  out.push({ name, current, historic, dist, jam, section_flag: flag });
}
return out;
"""
    try:
        return driver.execute_script(script)
    except Exception:
        return []

def _js_extraer_tramos_fallback(driver):
    """
    Fallback ultra-simple si el DOM cambia: calcula `section_flag` subiendo en
    la jerarquía (cruzando shadow roots) y toma textos mínimos.
    """
    script = r"""
function qText(el){ return ((el && (el.innerText || el.textContent)) || '').trim(); }
function climbToSection(el){
  let n = el, guard = 0;
  while (n && guard++ < 80){
    if (n.closest){ const sec = n.closest('app-traffic-view-sidebar-section'); if (sec) return sec; }
    const root = n.getRootNode ? n.getRootNode() : null;
    if (root && root.host){ n = root.host; continue; }
    n = n.parentElement;
  }
  return null;
}
function sectionFlagFor(el){
  const sec = climbToSection(el);
  if (!sec) return '';
  const cap = sec.querySelector('wz-caption') || (sec.shadowRoot ? sec.shadowRoot.querySelector('wz-caption') : null);
  const t = qText(cap).toLowerCase();
  if (/unusual|inusual/.test(t)) return 'unusual';
  if (/watchlist|lista de seguimiento/.test(t)) return 'watch';
  return '';
}
const routes = Array.from(document.querySelectorAll('app-traffic-view-route'));
const out = [];
for (const r of routes){
  const name = qText(r.querySelector('wz-subhead4')) || (r.shadowRoot ? qText(r.shadowRoot.querySelector('wz-subhead4')) : '');
  if (!name) continue;
  const curr = qText(r.querySelector('.current-stat')) || (r.shadowRoot ? qText(r.shadowRoot.querySelector('.current-stat')) : '');
  const hist = qText(r.querySelector('.historic-stat')) || (r.shadowRoot ? qText(r.shadowRoot.querySelector('.historic-stat')) : '');
  const dist = qText(r.querySelector('.route-distance')) || (r.shadowRoot ? qText(r.shadowRoot.querySelector('.route-distance')) : '');
  const jamEl = r.querySelector('.jam-level') || (r.shadowRoot ? r.shadowRoot.querySelector('.jam-level') : null);
  const m = jamEl && (jamEl.className || '').match(/jam-level-(\d)/);
  const jam = m ? parseInt(m[1],10) : null;
  const flag = sectionFlagFor(r);
  out.push({ name, current: curr, historic: hist, dist, jam, section_flag: flag });
}
return out;
"""
    try:
        return driver.execute_script(script)
    except Exception:
        return []


# ===================== Parsers robustos =====================
def _parse_minutos(texto: str) -> Optional[int]:
    if not texto:
        return None
    m = re.search(r"(\d+)\s*min(?:utos)?\b", texto, flags=re.I)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def _parse_vel_kmh(texto: str) -> Optional[float]:
    if not texto:
        return None
    m = re.search(r"([\d.,]+)\s*km/?h", texto, flags=re.I)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "."))
    except Exception:
        return None


def _parse_stat_to_min_vel(texto: str) -> Tuple[Optional[int], Optional[float]]:
    if not texto:
        return None, None
    return _parse_minutos(texto), _parse_vel_kmh(texto)


def _parse_dist_km(texto: str) -> Optional[float]:
    if not texto:
        return None
    m = re.search(r"([\d.,]+)\s*km\b", texto, flags=re.I)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "."))
    except Exception:
        return None


def _tiempo_desde_dist_y_vel(dist_km: Optional[float], vel_kmh: Optional[float]) -> Tuple[Optional[int], str]:
    try:
        if dist_km is None or vel_kmh is None or vel_kmh <= 0:
            return None, ""
        horas = dist_km / vel_kmh
        total_seg = int(round(horas * 3600))
        mm = total_seg // 60
        ss = total_seg % 60
        return total_seg, f"{mm:02d}:{ss:02d}"
    except Exception:
        return None, ""


# ===================== Detector =====================
class TrafficDetector:
    """
    - Scroll completo hasta cargar todas las tarjetas
    - Extracción vía JS del DOM (+ fallback)
    - Clasificación SOLO por la sección del panel (Unusual/Watchlist)
    """
    def __init__(self, cfg, logger):
        self.cfg = cfg
        self.logger = logger
        self.driver = None

    def start(self):
        self.driver = _build_driver(self.cfg)
        self.driver.get(self.cfg.waze_url)
        try:
            _activar_traffic_view(self.driver, self.log)
        except Exception as e:
            self.close()
            raise e
        # registrar este detector para poder cerrarlo desde la GUI
        try:
            _GLOBAL_DETECTORS.add(self)
        except Exception:
            pass
        global _LAST_DRIVER
        _LAST_DRIVER = self.driver

    def refresh(self):
        if self.driver:
            self.driver.refresh()
            _esperar_panel(self.driver, timeout=30)

    def detect_all(self) -> list:
        if not self.driver:
            raise RuntimeError("Detector no iniciado")

        _, _, _, _, _, TimeoutException, _ = _get_selenium_bits()

        try:
            _esperar_panel(self.driver, timeout=30)
        except TimeoutException:
            self.log("Panel lateral no apareció a tiempo")
            return []

        self.log("Iniciando scroll y extracción JS…")
        _cargar_lista_completa(self.driver, self.log)

        try:
            total_dom = self.driver.execute_script(
                "return document.querySelectorAll('app-traffic-view-route').length;"
            )
            self.log(f"[JS] Rutas en DOM tras scroll: {total_dom}")
        except Exception:
            pass

        brut = _js_extraer_tramos(self.driver)
        if not brut:
            self.log("[JS] _js_extraer_tramos devolvió 0; intentando fallback…")
            brut = _js_extraer_tramos_fallback(self.driver)
            if not brut:
                self.log("[JS] Fallback también devolvió 0 — verifica que el panel esté expandido (Traffic View activo).")
                return []

        vistos = set()
        lista = []

        # Import diferido para el dataclass y evitar ciclos
        from models import TramoNorm

        for r in brut:
            name = (r.get("name") or "").strip()
            dist_raw = (r.get("dist") or "").strip()
            curr = (r.get("current") or "").strip()
            hist = (r.get("historic") or "").strip()
            jam = r.get("jam")

            # CLAVE: usar SOLO la sección del panel para clasificar
            flag = (r.get("section_flag") or "").strip().lower()
            if flag == "unusual":
                es_usual = False
            elif flag == "watch":
                es_usual = True
            else:
                # Sin flag: por defecto False (puedes cambiar a None si lo prefieres)
                es_usual = None

            if not name:
                continue

            # Dedupe por (nombre, distancia, current/historic visible)
            clave_curr = curr if curr else (hist or "")
            clave = f"{name}||{dist_raw}||{clave_curr}"
            if clave in vistos:
                continue
            vistos.add(clave)

            # Parse de tiempos/velocidades/distancias
            min_c, vel_c = _parse_stat_to_min_vel(curr)
            min_h, vel_h = _parse_stat_to_min_vel(hist)
            minutos = min_c if min_c is not None else min_h
            vel = vel_c if vel_c is not None else vel_h
            d_km = _parse_dist_km(dist_raw)

            if isinstance(minutos, int):
                tiempo_seg = int(minutos * 60)
                tiempo_mmss = f"{minutos:02d}:00"
            else:
                tiempo_seg, tiempo_mmss = _tiempo_desde_dist_y_vel(d_km, vel)

            raw = f"{name} | {(curr or hist or '').strip()} | {dist_raw} | tiempo={tiempo_mmss or '--:--'}"

            lista.append(TramoNorm(
                nombre=name, dist_km=d_km, tiempo_min=minutos, tiempo_seg=tiempo_seg,
                tiempo_mmss=tiempo_mmss or "", vel_kmh=vel, jam=jam, es_usual=es_usual,
                current_raw=curr, historic_raw=hist, dist_raw=dist_raw, raw=raw
            ))

        self.log(f"[JS] Extraídos: {len(lista)} (usuales={sum(1 for x in lista if x.es_usual)} | inusuales={sum(1 for x in lista if not x.es_usual)})")
        return lista

    def close(self):
        global _LAST_DRIVER
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            if _LAST_DRIVER is self.driver:
                _LAST_DRIVER = None
            self.driver = None
        # remover del registro global
        try:
            _GLOBAL_DETECTORS.discard(self)
        except Exception:
            pass

    def log(self, msg: str):
        try:
            self.logger.info(msg)
        except Exception:
            pass


# ===================== API simple para captura única =====================
def detectar_con_driver(cfg) -> list:
    import logging
    logger = logging.getLogger("captura_waze")
    det = TrafficDetector(cfg, logger)
    det.start()
    try:
        return det.detect_all()
    finally:
        det.close()


# ===================== Singleton para GUI =====================
def _get_detector():
    """
    Crea (si hace falta) y retorna un TrafficDetector configurado desde `config`
    y `models.AppConfig`. Reutiliza el driver entre llamadas (GUI: crudos/captura).
    """
    global _DETECTOR_SINGLETON
    if _DETECTOR_SINGLETON is not None:
        return _DETECTOR_SINGLETON

    import logging
    from models import AppConfig
    import config as _cfg

    appcfg = AppConfig(
        excel_path=getattr(_cfg, "excel_path_default", "Captura_Waze.xlsx"),
        headless=bool(getattr(_cfg, "headless_default", False)),
        perfil_persistente=bool(getattr(_cfg, "perfil_persistente_default", True)),
        log_level=str(getattr(_cfg, "log_level_default", "INFO")),
        waze_url=str(getattr(_cfg, "WAZE_URL", "https://www.waze.com/partnerhub/map-tool"))
    )

    logger = logging.getLogger("captura_waze")
    det = TrafficDetector(appcfg, logger)
    det.start()
    _DETECTOR_SINGLETON = det
    return det


def detect_all_segments():
    """
    API esperada por la GUI para extraer TODOS los tramos (crudos),
    devolviendo una lista de diccionarios.
    """
    det = _get_detector()
    tramos = det.detect_all()
    out = []
    for t in tramos:
        out.append({
            "nombre": t.nombre,
            "dist_km": t.dist_km,
            "tiempo_min": t.tiempo_min,
            "tiempo_seg": t.tiempo_seg,
            "tiempo_mmss": t.tiempo_mmss,
            "vel_kmh": t.vel_kmh,
            "jam": t.jam,
            "es_usual": t.es_usual,
            "current_raw": t.current_raw,
            "historic_raw": t.historic_raw,
            "dist_raw": t.dist_raw,
            "raw": t.raw
        })
    return out


def capture_and_save():
    """
    Retorna: (guardados_total, usuales, inusuales)
    Ojo: desconocidos no se cuentan en guardados (coherente con storage).
    """
    tramos = detect_all_segments()
    usuales = sum(1 for t in tramos if t.get("es_usual") is True)
    inusuales = sum(1 for t in tramos if t.get("es_usual") is False)
    guardados = usuales + inusuales
    return (guardados, usuales, inusuales)

