// src/AdminPanel.jsx
import React, { useEffect, useMemo, useRef, useState } from "react";

/* ----------------- Config ----------------- */
// URL del backend (sin "/" final)
const API_URL = (process.env.REACT_APP_API_URL || "https://web-production-e7d2.up.railway.app")
  .replace(/\/$/, "");

/* ----------------- Auth header ----------------- */
const getAdminSecret = () => sessionStorage.getItem("adminSecret") || "";
const setAdminSecret = (s) => sessionStorage.setItem("adminSecret", s || "");

/* ----------------- HTTP helpers ----------------- */
async function apiGet(path) {
  const r = await fetch(`${API_URL}${path}`, {
    headers: { "x-admin-secret": getAdminSecret() },
  });
  if (!r.ok) throw new Error(`GET ${path} → ${r.status}`);
  return r.json();
}
async function apiJson(method, path, body) {
  const r = await fetch(`${API_URL}${path}`, {
    method,
    headers: {
      "Content-Type": "application/json",
      "x-admin-secret": getAdminSecret(),
    },
    body: JSON.stringify(body ?? {}),
  });
  if (!r.ok) {
    const t = await r.text().catch(() => "");
    throw new Error(`${method} ${path} → ${r.status} ${t}`);
  }
  return r.status === 204 ? { ok: true } : r.json();
}

const prettyMoney = (n) =>
  new Intl.NumberFormat("es-AR", { style: "currency", currency: "ARS" }).format(Number(n || 0));
const fmtDate = (s) => (s ? new Date(s).toLocaleString() : "—");

/* =====================================================
   AdminPanel (acordeón por dispenser, 6 slots fijos)
   ===================================================== */
export default function AdminPanel() {
  const [authOk, setAuthOk] = useState(!!getAdminSecret());
  const [checkingAuth, setCheckingAuth] = useState(false);

  // Config
  const [mpMode, setMpMode] = useState("test");
  const [umbralAlerta, setUmbralAlerta] = useState(null);
  const [stockReserva, setStockReserva] = useState(null);
  const live = mpMode === "live";

  // Dispensers
  const [dispensers, setDispensers] = useState([]); // [{id, device_id, nombre, activo}]
  // Productos por dispenser -> array de 6 posiciones (1..6)
  const [slotsByDisp, setSlotsByDisp] = useState({}); // { [dispId]: [slot1..slot6] }
  const [expanded, setExpanded] = useState({}); // acordeón

  // Estado de edición por fila (para no pisar con refresh al escribir)
  const editingRef = useRef({}); // { `${dispId}-${slot}`: true }

  // Pagos
  const [pagos, setPagos] = useState([]);
  const [pagosLoading, setPagosLoading] = useState(false);
  const [fltEstado, setFltEstado] = useState("");
  const [fltQ, setFltQ] = useState("");
  const pagosTimer = useRef(null);

  // QR modal
  const [qrLink, setQrLink] = useState("");
  const [showQR, setShowQR] = useState(false);

  /* ----------------- Auth simple ----------------- */
  const promptPassword = async () => {
    const pwd = window.prompt("Ingresá la contraseña de admin:");
    if (!pwd) return false;
    setAdminSecret(pwd);
    setCheckingAuth(true);
    try {
      await fetch(`${API_URL}/api/dispensers`, { headers: { "x-admin-secret": pwd } });
      setAuthOk(true);
      return true;
    } catch {
      alert("Contraseña inválida o backend inaccesible.");
      setAdminSecret("");
      setAuthOk(false);
      return false;
    } finally {
      setCheckingAuth(false);
    }
  };

  /* ----------------- Carga de config ----------------- */
  const loadConfig = async () => {
    try {
      const c = await apiGet("/api/config");
      setMpMode((c?.mp_mode || "test").toLowerCase());
      if (typeof c?.umbral_alta_lts === "number") setUmbralAlerta(c.umbral_alerta_lts);
      if (typeof c?.umbral_alerta_lts === "number") setUmbralAlerta(c.umbral_alerta_lts);
      if (typeof c?.stock_reserva_lts === "number") setStockReserva(c.stock_reserva_lts);
    } catch (e) {
      console.error(e);
    }
  };
  const setMode = async (mode) => {
    try {
      await apiJson("POST", "/api/mp/mode", { mode });
      await loadConfig();
    } catch (e) {
      alert(e.message);
    }
  };
  const toggleMode = () => setMode(live ? "test" : "live");

  /* ----------------- Dispensers + slots ----------------- */
  const loadDispensers = async () => {
    const ds = await apiGet("/api/dispensers");
    setDispensers(ds || []);
    const ex = {};
    (ds || []).forEach((d, i) => (ex[d.id] = i === 0)); // abre el primero
    setExpanded(ex);
  };

  const normalizeSix = (products) => {
    const map = {};
    (products || []).forEach((p) => (map[p.slot] = p));
    const arr = [];
    for (let s = 1; s <= 6; s++) {
      arr.push(
        map[s] || {
          // placeholder (slot vacío)
          id: null,
          dispenser_id: null,
          nombre: "",
          precio: "",
          cantidad: "",
          porcion_litros: "1",
          slot: s,
          habilitado: false,
          __placeholder: true,
        }
      );
    }
    return arr;
  };

  const loadProductosOf = async (dispId) => {
    try {
      const data = await apiGet(`/api/productos?dispenser_id=${dispId}`);
      const six = normalizeSix(data);
      // No pisar una fila si está en edición
      const keyPrefix = `${dispId}-`;
      const someoneEditing = Object.keys(editingRef.current).some(
        (k) => k.startsWith(keyPrefix) && editingRef.current[k]
      );
      if (!someoneEditing) {
        setSlotsByDisp((prev) => ({ ...prev, [dispId]: six }));
      }
    } catch (e) {
      console.error(e);
    }
  };

  const loadAllSlots = async () => {
    await Promise.all((dispensers || []).map((d) => loadProductosOf(d.id)));
  };

  /* ----------------- Pagos ----------------- */
  const loadPagos = async () => {
    setPagosLoading(true);
    try {
      const qs = new URLSearchParams();
      if (fltEstado) qs.set("estado", fltEstado);
      if (fltQ) qs.set("q", fltQ.trim());
      qs.set("limit", "10");
      const data = await apiGet(`/api/pagos?${qs.toString()}`);
      setPagos(data || []);
    } catch (e) {
      console.error(e);
    } finally {
      setPagosLoading(false);
    }
  };

  /* ----------------- Montaje ----------------- */
  useEffect(() => {
    if (!authOk) return;
    (async () => {
      await Promise.all([loadDispensers(), loadConfig()]);
    })();
    return () => {
      if (pagosTimer.current) clearInterval(pagosTimer.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [authOk]);

  // Cuando hay dispensers, cargo sus slots + empiezo auto-refresh de pagos
  useEffect(() => {
    if (!authOk || (dispensers || []).length === 0) return;
    loadAllSlots();
    loadPagos();
    if (pagosTimer.current) clearInterval(pagosTimer.current);
    pagosTimer.current = setInterval(loadPagos, 5000);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dispensers.length]);

  useEffect(() => {
    if (!authOk) return;
    loadPagos();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fltEstado]);

  /* ----------------- Helpers de stock crítico ----------------- */
  const isCriticalSlot = (row, reserva) => {
    if (!row || !row.id) return false;
    const stock = Number(row.cantidad ?? 0);
    if (Number.isNaN(stock)) return false;
    // Alerta simple: stock actual ≤ reserva
    return typeof reserva === "number" && stock <= reserva;
    // Si preferís “venta posible” como criterio:
    // const porcion = Math.max(1, Number(row.porcion_litros ?? 1));
    // return stock - porcion <= reserva;
  };

  /* ----------------- Handlers por slot ----------------- */
  const setEditing = (dispId, slot, v) => {
    const k = `${dispId}-${slot}`;
    editingRef.current[k] = v;
  };

  const updateSlotField = (dispId, slot, field, value) => {
    setSlotsByDisp((prev) => {
      const arr = prev[dispId] ? [...prev[dispId]] : [];
      const idx = slot - 1;
      if (!arr[idx]) return prev;
      arr[idx] = { ...arr[idx], [field]: value };
      return { ...prev, [dispId]: arr };
    });
  };

  const saveSlot = (disp, slotIdx) => async () => {
    const slotNum = slotIdx + 1;
    const row = (slotsByDisp[disp.id] || [])[slotIdx];
    if (!row) return;

    try {
      const payload = {
        nombre: String(row.nombre || "").trim(),
        precio: Number(row.precio || 0),
        cantidad: Number(row.cantidad || 0),
        porcion_litros: Math.max(1, Number(row.porcion_litros || 1)),
        habilitado: Boolean(row.habilitado),
      };

      if (row.__placeholder || !row.id) {
        // Crear producto en ese slot fijo
        const res = await apiJson("POST", `/api/productos`, {
          dispenser_id: disp.id,
          slot: slotNum,
          ...payload,
        });
        const p = res?.producto;
        if (p) {
          setSlotsByDisp((prev) => {
            const arr = [...(prev[disp.id] || [])];
            arr[slotIdx] = p;
            return { ...prev, [disp.id]: arr };
          });
          alert("Slot creado/guardado");
        }
      } else {
        // Actualizar producto existente
        const res = await apiJson("PUT", `/api/productos/${row.id}`, payload);
        const p = res?.producto;
        if (p) {
          setSlotsByDisp((prev) => {
            const arr = [...(prev[disp.id] || [])];
            arr[slotIdx] = p;
            return { ...prev, [disp.id]: arr };
          });
          alert("Cambios guardados");
        }
      }
    } catch (e) {
      alert(e.message);
    } finally {
      setEditing(disp.id, slotNum, false);
      // refrescar para reflejar thresholds/habilitado que pudo cambiar por stock
      await loadProductosOf(disp.id);
    }
  };

  const reponer = (disp, slotIdx) => async () => {
    const row = (slotsByDisp[disp.id] || [])[slotIdx];
    if (!row?.id) return alert("Primero guardá el producto del slot");
    const litros = Number(prompt("¿Cuántos litros querés reponer? (ej: 5)") || 0);
    if (!litros || litros <= 0) return;
    try {
      const res = await apiJson("POST", `/api/productos/${row.id}/reponer`, { litros });
      const p = res?.producto;
      if (p) {
        setSlotsByDisp((prev) => {
          const arr = [...(prev[disp.id] || [])];
          arr[slotIdx] = p;
          return { ...prev, [disp.id]: arr };
        });
      }
    } catch (e) {
      alert(e.message);
    }
  };

  const resetStock = (disp, slotIdx) => async () => {
    const row = (slotsByDisp[disp.id] || [])[slotIdx];
    if (!row?.id) return alert("Primero guardá el producto del slot");
    const litros = Number(prompt("Setear stock exacto en litros (ej: 20)") || 0);
    if (litros < 0) return;
    try {
      const res = await apiJson("POST", `/api/productos/${row.id}/reset_stock`, { litros });
      const p = res?.producto;
      if (p) {
        setSlotsByDisp((prev) => {
          const arr = [...(prev[disp.id] || [])];
          arr[slotIdx] = p;
          return { ...prev, [disp.id]: arr };
        });
      }
    } catch (e) {
      alert(e.message);
    }
  };

  const toggleHabilitado = (disp, slotIdx) => async (checked) => {
    const row = (slotsByDisp[disp.id] || [])[slotIdx];
    if (!row?.id) return alert("Primero guardá el producto del slot");
    try {
      const res = await apiJson("PUT", `/api/productos/${row.id}`, { habilitado: !!checked });
      const p = res?.producto;
      if (p) {
        setSlotsByDisp((prev) => {
          const arr = [...(prev[disp.id] || [])];
          arr[slotIdx] = p;
          return { ...prev, [disp.id]: arr };
        });
      }
    } catch (e) {
      alert(e.message);
    }
  };

  // QR fijo /go (por slot → por product_id fijo)
  const mostrarQRFijo = (row) => () => {
    if (!row?.id) return alert("Primero guardá el producto del slot");
    const link = `${API_URL}/go?pid=${row.id}`;
    setQrLink(link);
    setShowQR(true);
  };

  // Reenviar orden (tabla pagos)
  const reenviarPago = async (id) => {
    try {
      const res = await apiJson("POST", `/api/pagos/${id}/reenviar`);
      alert(res.msg || "Reenvío enviado");
      await loadPagos();
    } catch (e) {
      alert("Error reintentando: " + e.message);
    }
  };

  const qrImg = useMemo(() => {
    if (!qrLink) return "";
    return `https://api.qrserver.com/v1/create-qr-code/?size=220x220&data=${encodeURIComponent(
      qrLink
    )}`;
  }, [qrLink]);

  /* ----------------- Render ----------------- */
  if (!authOk) {
    return (
      <div style={styles.page}>
        <div style={{ ...styles.card, maxWidth: 480, margin: "100px auto" }}>
          <h1 style={styles.title}>Dispen-Easy · Admin</h1>
          <p style={styles.subtitle}>
            Backend: <code>{API_URL}</code>
          </p>
          <button
            style={{ ...styles.primaryBtn, width: "100%", marginTop: 12 }}
            onClick={promptPassword}
            disabled={checkingAuth}
          >
            {checkingAuth ? "Ingresando…" : "Ingresar"}
          </button>
          <p style={{ opacity: 0.7, fontSize: 12, marginTop: 8 }}>
            Esta contraseña viaja como header <code>x-admin-secret</code>.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div style={styles.page}>
      <header style={styles.header}>
        <div>
          <h1 style={{ ...styles.title, color: live ? "#10b981" : "#e5e7eb" }}>
            Dispen-Easy · Administración
          </h1>
          <div style={styles.subtitle}>Backend: <code>{API_URL}</code></div>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <button style={styles.reloadBtn} onClick={loadPagos} disabled={pagosLoading}>
            {pagosLoading ? "Pagos…" : "Actualizar pagos"}
          </button>
        </div>
      </header>

      {/* Modo de pago */}
      <section style={styles.card}>
        <h2 style={styles.h2}>Modo de pago</h2>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <span
            style={{
              padding: "6px 12px",
              borderRadius: 999,
              fontWeight: 800,
              background: live ? "#10b981" : "#f59e0b",
              color: live ? "#05251c" : "#2a1702",
            }}
            title={live ? "Producción (pagos reales)" : "Test (sandbox)"}
          >
            {live ? "PROD" : "TEST"}
          </span>
          <button
            style={styles.secondaryBtn}
            onClick={toggleMode}
            title={live ? "Pasar a Test" : "Pasar a Producción"}
          >
            {live ? "Pasar a Test" : "Pasar a Producción"}
          </button>
          <div style={{ marginLeft: 12, opacity: 0.8, fontSize: 12 }}>
            Umbral alerta: <b>{umbralAlerta ?? "—"} L</b> · Reserva crítica:{" "}
            <b>{stockReserva ?? "—"} L</b>
          </div>
        </div>
      </section>

      {/* Acordeón de dispensers (cada uno con 6 slots fijos) */}
      {dispensers.map((disp) => {
        const rows = slotsByDisp[disp.id] || normalizeSix([]);
        const hasCritical = rows.some((row) => isCriticalSlot(row, stockReserva));

        return (
          <section key={disp.id} style={styles.card}>
            <div
              style={styles.dispHeader}
              onClick={() => setExpanded((e) => ({ ...e, [disp.id]: !e[disp.id] }))}
            >
              <div style={styles.dispTitle}>
                <span style={styles.dispBadge}>{disp.device_id}</span>
                <b style={hasCritical ? styles.dispTitleCritical : undefined}>
                  {disp.nombre || `Dispenser ${disp.id}`}
                </b>
              </div>
              <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                <span style={disp.activo ? styles.ok : styles.warn}>
                  {disp.activo ? "Activo" : "Suspendido"}
                </span>
                <button
                  style={styles.secondaryBtn}
                  onClick={(e) => {
                    e.stopPropagation();
                    apiJson("PUT", `/api/dispensers/${disp.id}`, { activo: !disp.activo })
                      .then((res) =>
                        setDispensers((list) =>
                          list.map((d) => (d.id === disp.id ? { ...d, ...res.dispenser } : d))
                        )
                      )
                      .catch((err) => alert(err.message));
                  }}
                >
                  {disp.activo ? "Suspender" : "Activar"}
                </button>
                <button
                  style={styles.secondaryBtn}
                  onClick={(e) => {
                    e.stopPropagation();
                    loadProductosOf(disp.id);
                  }}
                >
                  Refrescar
                </button>
              </div>
            </div>

            {expanded[disp.id] && (
              <div style={{ overflowX: "auto", marginTop: 10 }}>
                <table style={styles.table}>
                  <thead>
                    <tr>
                      <th>Slot</th>
                      <th>Nombre</th>
                      <th>$ por L</th>
                      <th>Porción (L)</th>
                      <th>Stock (L)</th>
                      <th>Activo</th>
                      <th>Acciones</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((row, idx) => {
                      const slotNum = idx + 1;
                      const k = `${disp.id}-${slotNum}`;
                      return (
                        <tr
                          key={k}
                          onFocus={() => setEditing(disp.id, slotNum, true)}
                          onBlur={() => setEditing(disp.id, slotNum, false)}
                        >
                          <td><b>{slotNum}</b></td>
                          <td>
                            <input
                              style={styles.inputInline}
                              placeholder="Nombre"
                              value={row.nombre ?? ""}
                              onChange={(e) =>
                                updateSlotField(disp.id, slotNum, "nombre", e.target.value)
                              }
                            />
                          </td>
                          <td>
                            <input
                              style={styles.inputInline}
                              type="number"
                              step="0.01"
                              placeholder="Precio"
                              value={row.precio ?? ""}
                              onChange={(e) =>
                                updateSlotField(disp.id, slotNum, "precio", e.target.value)
                              }
                            />
                          </td>
                          <td>
                            <input
                              style={styles.inputInline}
                              type="number"
                              step="1"
                              min="1"
                              placeholder="Porción"
                              value={row.porcion_litros ?? "1"}
                              onChange={(e) =>
                                updateSlotField(disp.id, slotNum, "porcion_litros", e.target.value)
                              }
                            />
                          </td>
                          <td>
                            <input
                              style={styles.inputInline}
                              type="number"
                              step="1"
                              placeholder="Stock"
                              value={row.cantidad ?? ""}
                              onChange={(e) =>
                                updateSlotField(disp.id, slotNum, "cantidad", e.target.value)
                              }
                            />
                          </td>
                          <td>
                            <Toggle
                              checked={!!row.habilitado}
                              onChange={toggleHabilitado(disp, idx)}
                            />
                          </td>
                          <td>
                            <div style={styles.actions}>
                              <button style={styles.primaryBtn} onClick={saveSlot(disp, idx)}>
                                Guardar
                              </button>
                              <button style={styles.secondaryBtn} onClick={reponer(disp, idx)}>
                                Reponer
                              </button>
                              <button style={styles.secondaryBtn} onClick={resetStock(disp, idx)}>
                                Reset
                              </button>
                              <button style={styles.qrBtn} onClick={mostrarQRFijo(row)}>
                                QR fijo (/go)
                              </button>
                              {row?.id ? (
                                <span style={{ fontSize: 12, opacity: 0.75 }}>
                                  pid: <code>{row.id}</code>
                                </span>
                              ) : (
                                <span style={{ fontSize: 12, opacity: 0.6 }}>sin producto</span>
                              )}
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        );
      })}

      {/* Pagos recientes */}
      <section style={styles.card}>
        <h2 style={styles.h2}>Pagos recientes (últimos 10)</h2>
        <div style={{ display: "flex", gap: 8, marginBottom: 10, alignItems: "center" }}>
          <label>
            Estado:&nbsp;
            <select
              value={fltEstado}
              onChange={(e) => setFltEstado(e.target.value)}
              style={styles.inputInline}
            >
              <option value="">Todos</option>
              <option value="approved">Approved</option>
              <option value="pending">Pending</option>
              <option value="rejected">Rejected</option>
            </select>
          </label>
          <input
            style={{ ...styles.inputInline, maxWidth: 260 }}
            placeholder="Buscar por mp_payment_id"
            value={fltQ}
            onChange={(e) => setFltQ(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && loadPagos()}
          />
          <button style={styles.secondaryBtn} onClick={loadPagos} disabled={pagosLoading}>
            {pagosLoading ? "Buscando…" : "Buscar / Actualizar pagos"}
          </button>
        </div>
        <div style={{ overflowX: "auto" }}>
          <table style={styles.table}>
            <thead>
              <tr>
                <th>ID</th>
                <th>mp_payment_id</th>
                <th>Estado</th>
                <th>Litros</th>
                <th>Monto</th>
                <th>Slot</th>
                <th>Producto ID</th>
                <th>Disp</th>
                <th>Fecha</th>
                <th>Acciones</th>
              </tr>
            </thead>
            <tbody>
              {pagos.length === 0 && (
                <tr>
                  <td colSpan={10} style={styles.empty}>
                    {pagosLoading ? "Cargando…" : "Sin pagos para mostrar"}
                  </td>
                </tr>
              )}
              {pagos.map((p) => {
                const puedeReintentar =
                  p.estado === "approved" && !p.dispensado && p.slot_id > 0 && p.litros > 0;
                return (
                  <tr key={p.id}>
                    <td>{p.id}</td>
                    <td style={{ fontFamily: "monospace" }}>{p.mp_payment_id}</td>
                    <td><span style={badgeFor(p.estado)}>{p.estado}</span></td>
                    <td>{p.litros}</td>
                    <td>{prettyMoney(p.monto)}</td>
                    <td>{p.slot_id}</td>
                    <td>{p.product_id}</td>
                    <td style={{ fontFamily: "monospace", fontSize: 12 }}>{p.device_id || "—"}</td>
                    <td>{fmtDate(p.created_at)}</td>
                    <td>
                      <button
                        style={{
                          ...styles.secondaryBtn,
                          opacity: puedeReintentar ? 1 : 0.5,
                          cursor: puedeReintentar ? "pointer" : "not-allowed",
                        }}
                        onClick={() => puedeReintentar && reenviarPago(p.id)}
                        disabled={!puedeReintentar}
                        title={
                          puedeReintentar
                            ? "Reenviar orden al ESP"
                            : "Solo para pagos approved y no dispensados"
                        }
                      >
                        Reintentar
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>

      {/* Modal QR */}
      {showQR && (
        <div style={styles.modalBackdrop} onClick={() => setShowQR(false)}>
          <div style={styles.modal} onClick={(e) => e.stopPropagation()}>
            <h3 style={{ margin: 0, marginBottom: 8 }}>Link</h3>
            <p style={{ margin: 0, marginBottom: 12, wordBreak: "break-all" }}>{qrLink}</p>
            {qrImg && <img src={qrImg} alt="QR" style={{ width: 220, height: 220, borderRadius: 8 }} />}
            <div style={{ marginTop: 12, display: "flex", gap: 8 }}>
              <a href={qrLink} target="_blank" rel="noreferrer" style={styles.primaryBtn}>
                Abrir link
              </a>
              <button style={styles.secondaryBtn} onClick={() => setShowQR(false)}>
                Cerrar
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

/* ----------------- UI helpers ----------------- */
function Toggle({ checked, onChange }) {
  return (
    <label style={styles.switch}>
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      <span style={styles.slider} />
    </label>
  );
}
function badgeFor(status) {
  const base = {
    padding: "4px 8px",
    borderRadius: 999,
    fontSize: 12,
    fontWeight: 700,
    textTransform: "uppercase",
    letterSpacing: 0.3,
  };
  if (status === "approved") return { ...base, background: "#10b981", color: "#06251d" };
  if (status === "pending") return { ...base, background: "#f59e0b", color: "#2a1702" };
  if (status === "rejected") return { ...base, background: "#ef4444", color: "#2a0a0a" };
  return { ...base, background: "#334155", color: "#e5e7eb" };
}

/* ----------------- Estilos ----------------- */
const styles = {
  page: {
    fontFamily: "Inter, system-ui, -apple-system, Segoe UI, Roboto, Arial",
    background: "#0b1220",
    minHeight: "100vh",
    color: "#e5e7eb",
    padding: 24,
  },
  header: {
    display: "flex",
    alignItems: "flex-end",
    justifyContent: "space-between",
    marginBottom: 16,
  },
  title: { margin: 0, fontSize: 24, fontWeight: 700 },
  subtitle: { margin: "4px 0 0", opacity: 0.8, fontSize: 12 },
  reloadBtn: {
    background: "#334155",
    border: "1px solid #475569",
    color: "#e5e7eb",
    padding: "8px 12px",
    borderRadius: 10,
    cursor: "pointer",
  },
  h2: { margin: "0 0 12px", fontSize: 18 },
  card: {
    background: "rgba(255,255,255,0.04)",
    border: "1px solid rgba(255,255,255,0.08)",
    borderRadius: 16,
    padding: 16,
    marginBottom: 16,
    boxShadow: "0 5px 20px rgba(0,0,0,0.25)",
  },
  ok: { color: "#10b981", fontWeight: 700 },
  warn: { color: "#f59e0b", fontWeight: 700 },

  dispHeader: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    cursor: "pointer",
  },
  dispTitle: { display: "flex", alignItems: "center", gap: 8, fontSize: 18, marginBottom: 2 },
  dispTitleCritical: { color: "#f59e0b" }, // <--- amarillo si hay stock crítico
  dispBadge: {
    background: "#1f2937",
    border: "1px solid #374151",
    color: "#e5e7eb",
    fontSize: 12,
    padding: "2px 8px",
    borderRadius: 999,
  },

  table: { width: "100%", borderCollapse: "separate", borderSpacing: 0 },
  inputInline: {
    background: "#0f172a",
    border: "1px solid #334155",
    color: "#e5e7eb",
    padding: "6px 8px",
    borderRadius: 8,
    outline: "none",
    width: "100%",
  },
  actions: { display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" },
  primaryBtn: {
    background: "#10b981",
    border: "none",
    color: "#06251d",
    padding: "8px 10px",
    borderRadius: 10,
    cursor: "pointer",
    fontWeight: 700,
  },
  secondaryBtn: {
    background: "#1f2937",
    border: "1px solid #374151",
    color: "#e5e7eb",
    padding: "8px 10px",
    borderRadius: 10,
    cursor: "pointer",
  },
  qrBtn: {
    background: "#3b82f6",
    border: "none",
    color: "#061528",
    padding: "8px 10px",
    borderRadius: 10,
    cursor: "pointer",
    fontWeight: 700,
  },

  switch: {
    position: "relative",
    display: "inline-block",
    width: 44,
    height: 24,
  },
  slider: {
    position: "absolute",
    cursor: "pointer",
    inset: 0,
    background: "#374151",
    borderRadius: 999,
    transition: "0.2s",
  },
};
