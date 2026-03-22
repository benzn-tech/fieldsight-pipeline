import { useState, useRef, useEffect, useCallback } from "react";

/* ─── Config ─── */
const CONFIG = {
  apiBase: "",  // same origin via CloudFront proxy
  cognitoRegion: "ap-southeast-2",
  cognitoClientId: "5npb81jbj1hgh9tsck25kan3os",
};

/* ─── Theme ─── */
const T = {
  bg: "#F2F5FA", surface: "#FFFFFF", surfaceAlt: "#EDF1F7",
  primary: "#1B3A5C", primaryLight: "#2A5F8F", primaryFaded: "#1B3A5C0C",
  accent: "#D4740E", accentLight: "#D4740E10",
  text: "#1A2332", textSecondary: "#5A6B7F", textTertiary: "#94A5B9",
  border: "#D1DAE6", borderLight: "#E4EAF2",
  success: "#1D7A3F", successBg: "#1D7A3F0F",
  danger: "#C0392B", dangerBg: "#C0392B0C",
  warning: "#D4740E", warningBg: "#D4740E0C",
  font: "'IBM Plex Sans', sans-serif", mono: "'IBM Plex Mono', monospace",
  navBg: "#0F2640", navText: "#7D9AB8", navActive: "#FFFFFF",
  shadow: "0 1px 3px rgba(27,58,92,0.06)", inputBg: "#F7F9FC",
};

const topicColors = {
  safety: { label: "Safety", icon: "⚠", bg: T.dangerBg, color: T.danger },
  progress: { label: "Progress", icon: "●", bg: T.successBg, color: T.success },
  quality: { label: "Quality", icon: "◆", bg: `${T.primary}0D`, color: T.primary },
  compliance: { label: "Compliance", icon: "✓", bg: `${T.primary}0D`, color: T.primary },
};

/* ═══════════════════════════════════════════════ */
/* COGNITO AUTH (direct API, no SDK)              */
/* ═══════════════════════════════════════════════ */

const COGNITO_URL = `https://cognito-idp.${CONFIG.cognitoRegion}.amazonaws.com/`;

async function cognitoCall(action, payload) {
  const res = await fetch(COGNITO_URL, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-amz-json-1.1",
      "X-Amz-Target": `AWSCognitoIdentityProviderService.${action}`,
    },
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  if (data.__type) throw new Error(data.message || data.__type);
  return data;
}

async function signIn(email, password) {
  const result = await cognitoCall("InitiateAuth", {
    AuthFlow: "USER_PASSWORD_AUTH",
    ClientId: CONFIG.cognitoClientId,
    AuthParameters: { USERNAME: email, PASSWORD: password },
  });

  if (result.ChallengeName === "NEW_PASSWORD_REQUIRED") {
    return { challenge: "NEW_PASSWORD_REQUIRED", session: result.Session, email };
  }

  return {
    idToken: result.AuthenticationResult.IdToken,
    accessToken: result.AuthenticationResult.AccessToken,
    refreshToken: result.AuthenticationResult.RefreshToken,
  };
}

async function completeNewPassword(email, newPassword, session) {
  const result = await cognitoCall("RespondToAuthChallenge", {
    ChallengeName: "NEW_PASSWORD_REQUIRED",
    ClientId: CONFIG.cognitoClientId,
    ChallengeResponses: { USERNAME: email, NEW_PASSWORD: newPassword },
    Session: session,
  });
  return {
    idToken: result.AuthenticationResult.IdToken,
    accessToken: result.AuthenticationResult.AccessToken,
    refreshToken: result.AuthenticationResult.RefreshToken,
  };
}

function parseJwt(token) {
  try {
    const base64Url = token.split(".")[1];
    const base64 = base64Url.replace(/-/g, "+").replace(/_/g, "/");
    return JSON.parse(atob(base64));
  } catch { return {}; }
}


/* ═══════════════════════════════════════════════ */
/* API CLIENT                                     */
/* ═══════════════════════════════════════════════ */

async function api(path, token, options = {}) {
  const headers = { "Content-Type": "application/json" };
  if (token) headers["Authorization"] = token;

  const res = await fetch(`${CONFIG.apiBase}${path}`, { ...options, headers });
  if (res.status === 401) throw new Error("UNAUTHORIZED");
  const data = await res.json();
  if (res.ok) return data;
  throw new Error(data.error || data.message || `HTTP ${res.status}`);
}


/* ═══════════════════════════════════════════════ */
/* LOGIN SCREEN                                   */
/* ═══════════════════════════════════════════════ */

function LoginScreen({ onLogin }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [newPw, setNewPw] = useState("");
  const [challenge, setChallenge] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const handleLogin = async (e) => {
    e.preventDefault();
    setError(""); setLoading(true);
    try {
      const result = await signIn(email, password);
      if (result.challenge === "NEW_PASSWORD_REQUIRED") {
        setChallenge(result);
      } else {
        onLogin(result);
      }
    } catch (err) {
      setError(err.message);
    }
    setLoading(false);
  };

  const handleNewPw = async (e) => {
    e.preventDefault();
    setError(""); setLoading(true);
    try {
      const result = await completeNewPassword(challenge.email, newPw, challenge.session);
      onLogin(result);
    } catch (err) {
      setError(err.message);
    }
    setLoading(false);
  };

  const inputStyle = {
    width: "100%", padding: "12px 14px", fontSize: 14, border: `1px solid ${T.border}`,
    borderRadius: 8, background: T.inputBg, fontFamily: T.font, color: T.text,
    outline: "none", boxSizing: "border-box",
  };

  return (
    <div style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", background: `linear-gradient(135deg, ${T.navBg} 0%, #1a3a5c 100%)`, fontFamily: T.font }}>
      <div style={{ background: T.surface, borderRadius: 16, padding: "40px 36px", width: 380, boxShadow: "0 20px 60px rgba(0,0,0,0.3)" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 32 }}>
          <div style={{ width: 36, height: 36, borderRadius: 8, background: T.accent, display: "flex", alignItems: "center", justifyContent: "center", fontSize: 16, color: "#fff", fontWeight: 800 }}>S</div>
          <div>
            <div style={{ fontSize: 20, fontWeight: 700, color: T.text }}>SiteSync</div>
            <div style={{ fontSize: 11, color: T.textTertiary }}>Construction site documentation</div>
          </div>
        </div>

        {!challenge ? (
          <form onSubmit={handleLogin}>
            <div style={{ marginBottom: 14 }}>
              <label style={{ fontSize: 11, fontWeight: 600, color: T.textSecondary, marginBottom: 4, display: "block" }}>Email</label>
              <input type="email" value={email} onChange={e => setEmail(e.target.value)} style={inputStyle} placeholder="you@company.co.nz" required />
            </div>
            <div style={{ marginBottom: 20 }}>
              <label style={{ fontSize: 11, fontWeight: 600, color: T.textSecondary, marginBottom: 4, display: "block" }}>Password</label>
              <input type="password" value={password} onChange={e => setPassword(e.target.value)} style={inputStyle} placeholder="Enter password" required />
            </div>
            {error && <div style={{ fontSize: 12, color: T.danger, marginBottom: 12, padding: "8px 12px", background: T.dangerBg, borderRadius: 6 }}>{error}</div>}
            <button type="submit" disabled={loading} style={{
              width: "100%", padding: "12px 0", background: loading ? T.textTertiary : T.primary, color: "#fff",
              border: "none", borderRadius: 8, fontSize: 14, fontWeight: 600, cursor: loading ? "wait" : "pointer", fontFamily: T.font,
            }}>{loading ? "Signing in..." : "Sign In"}</button>
          </form>
        ) : (
          <form onSubmit={handleNewPw}>
            <div style={{ fontSize: 13, color: T.textSecondary, marginBottom: 16, lineHeight: 1.5 }}>
              First login detected. Please set a new password.
            </div>
            <div style={{ marginBottom: 20 }}>
              <label style={{ fontSize: 11, fontWeight: 600, color: T.textSecondary, marginBottom: 4, display: "block" }}>New Password</label>
              <input type="password" value={newPw} onChange={e => setNewPw(e.target.value)} style={inputStyle} placeholder="Min 8 chars, uppercase + number" required minLength={8} />
            </div>
            {error && <div style={{ fontSize: 12, color: T.danger, marginBottom: 12, padding: "8px 12px", background: T.dangerBg, borderRadius: 6 }}>{error}</div>}
            <button type="submit" disabled={loading} style={{
              width: "100%", padding: "12px 0", background: loading ? T.textTertiary : T.accent, color: "#fff",
              border: "none", borderRadius: 8, fontSize: 14, fontWeight: 600, cursor: loading ? "wait" : "pointer", fontFamily: T.font,
            }}>{loading ? "Setting password..." : "Set New Password"}</button>
          </form>
        )}
      </div>
    </div>
  );
}


/* ═══════════════════════════════════════════════ */
/* CALENDAR                                       */
/* ═══════════════════════════════════════════════ */

function CalendarPicker({ selectedDate, onSelect, onClose, dateData }) {
  const sel = new Date(selectedDate + "T12:00:00");
  const [viewYear, setViewYear] = useState(sel.getFullYear());
  const [viewMonth, setViewMonth] = useState(sel.getMonth());
  const today = new Date(); today.setHours(12,0,0,0);
  const todayStr = today.toISOString().slice(0,10);

  const daysInMonth = new Date(viewYear, viewMonth + 1, 0).getDate();
  const firstDay = new Date(viewYear, viewMonth, 1).getDay();
  const startOffset = firstDay === 0 ? 6 : firstDay - 1;
  const monthNames = ["January","February","March","April","May","June","July","August","September","October","November","December"];

  const cells = [];
  for (let i = 0; i < startOffset; i++) cells.push(null);
  for (let d = 1; d <= daysInMonth; d++) cells.push(d);

  return (
    <div style={{ position: "absolute", top: "100%", left: 0, marginTop: 4, zIndex: 100, background: T.surface, borderRadius: 12, border: `1px solid ${T.border}`, boxShadow: "0 8px 30px rgba(27,58,92,0.15)", padding: 16, width: 300 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
        <button onClick={() => viewMonth === 0 ? (setViewYear(viewYear-1), setViewMonth(11)) : setViewMonth(viewMonth-1)} style={{ background: T.inputBg, border: `1px solid ${T.borderLight}`, borderRadius: 6, padding: "4px 10px", cursor: "pointer", color: T.textSecondary, fontSize: 14 }}>‹</button>
        <span style={{ fontSize: 14, fontWeight: 700, color: T.text }}>{monthNames[viewMonth]} {viewYear}</span>
        <button onClick={() => viewMonth === 11 ? (setViewYear(viewYear+1), setViewMonth(0)) : setViewMonth(viewMonth+1)} style={{ background: T.inputBg, border: `1px solid ${T.borderLight}`, borderRadius: 6, padding: "4px 10px", cursor: "pointer", color: T.textSecondary, fontSize: 14 }}>›</button>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(7, 1fr)", gap: 2 }}>
        {["Mo","Tu","We","Th","Fr","Sa","Su"].map(d => (
          <div key={d} style={{ textAlign: "center", fontSize: 9, fontWeight: 700, color: T.textTertiary, padding: 4, letterSpacing: 0.5 }}>{d}</div>
        ))}
        {cells.map((day, i) => {
          if (!day) return <div key={`e${i}`} />;
          const dateStr = `${viewYear}-${String(viewMonth+1).padStart(2,"0")}-${String(day).padStart(2,"0")}`;
          const data = dateData[dateStr];
          const isSelected = dateStr === selectedDate;
          const isToday = dateStr === todayStr;
          const isFuture = dateStr > todayStr;
          return (
            <div key={dateStr} onClick={() => !isFuture && (onSelect(dateStr), onClose())}
              style={{ textAlign: "center", padding: "6px 0", borderRadius: 8, cursor: isFuture ? "default" : "pointer",
                background: isSelected ? T.primary : isToday ? T.primaryFaded : "transparent",
                color: isSelected ? "#fff" : isFuture ? T.borderLight : T.text, fontSize: 12, fontWeight: isSelected || isToday ? 700 : 500,
                position: "relative", border: isToday && !isSelected ? `1px solid ${T.primary}40` : "1px solid transparent",
              }}>
              {day}
              <div style={{ display: "flex", gap: 2, justifyContent: "center", marginTop: 2, height: 5 }}>
                {data?.hasReport && <div style={{ width: 5, height: 5, borderRadius: "50%", background: isSelected ? "#fff" : T.success }} />}
                {data?.safety > 0 && <div style={{ width: 5, height: 5, borderRadius: "50%", background: isSelected ? "#FFB4B4" : T.danger }} />}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}


/* ═══════════════════════════════════════════════ */
/* SHARED COMPONENTS                              */
/* ═══════════════════════════════════════════════ */

function Pill({ label, bg, color }) {
  return <span style={{ fontSize: 10, fontWeight: 600, padding: "3px 9px", borderRadius: 12, background: bg || T.surface, border: bg ? "none" : `1px solid ${T.borderLight}`, color: color || T.text }}>{label}</span>;
}
function Section({ title, subtitle, children }) {
  return <div style={{ marginBottom: 20 }}><div style={{ fontSize: 10, fontWeight: 700, color: T.textTertiary, letterSpacing: 0.8, marginBottom: 8 }}>{title}{subtitle && <span style={{ fontWeight: 400, letterSpacing: 0, marginLeft: 6, fontSize: 9 }}>— {subtitle}</span>}</div>{children}</div>;
}
function Card({ children, borderColor }) {
  return <div style={{ background: T.surface, borderRadius: 10, padding: "14px 16px", border: `1px solid ${borderColor || T.borderLight}`, borderLeft: borderColor ? `3px solid ${borderColor}` : undefined, boxShadow: T.shadow }}>{children}</div>;
}


/* ═══════════════════════════════════════════════ */
/* NAV BAR                                        */
/* ═══════════════════════════════════════════════ */

function NavBar({ tab, setTab, user, onLogout }) {
  const initials = (user.name || "?").split(" ").map(w => w[0]).join("").slice(0,2).toUpperCase();
  return (
    <div style={{ background: T.navBg, height: 48, display: "flex", alignItems: "center", justifyContent: "space-between", padding: "0 20px", flexShrink: 0 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <div style={{ width: 28, height: 28, borderRadius: 6, background: T.accent, display: "flex", alignItems: "center", justifyContent: "center", fontSize: 13, color: "#fff", fontWeight: 800 }}>S</div>
        <span style={{ color: T.navActive, fontSize: 15, fontWeight: 700 }}>SiteSync</span>
      </div>
      <div style={{ display: "flex", gap: 2, background: `${T.navText}15`, borderRadius: 8, padding: 2 }}>
        {[["day", "📋 My Day"], ["reports", "📄 Reports"]].map(([k, l]) => (
          <button key={k} onClick={() => setTab(k)} style={{
            background: tab === k ? `${T.navActive}18` : "transparent", color: tab === k ? T.navActive : T.navText,
            border: "none", fontSize: 11, fontWeight: 600, padding: "5px 16px", borderRadius: 6, cursor: "pointer",
          }}>{l}</button>
        ))}
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <div style={{ width: 28, height: 28, borderRadius: "50%", background: T.primaryLight, display: "flex", alignItems: "center", justifyContent: "center", fontSize: 10, color: "#fff", fontWeight: 700 }}>{initials}</div>
          <div>
            <div style={{ fontSize: 11, fontWeight: 600, color: T.navActive }}>{user.name}</div>
            <div style={{ fontSize: 9, color: T.navText }}>{user.email}</div>
          </div>
        </div>
        <button onClick={onLogout} style={{ background: "none", border: "none", color: T.navText, fontSize: 10, cursor: "pointer", padding: "4px 8px", borderRadius: 4 }} title="Sign out">↪ Out</button>
      </div>
    </div>
  );
}


/* ═══════════════════════════════════════════════ */
/* LEFT PANEL                                     */
/* ═══════════════════════════════════════════════ */

function LeftPanel({ report, loading, error, selected, setSelected, hidden, toggleHide, selectedDate, setSelectedDate, dateData, token, onRegenerate }) {
  const [calOpen, setCalOpen] = useState(false);
  const calRef = useRef(null);

  useEffect(() => {
    const handler = (e) => { if (calRef.current && !calRef.current.contains(e.target)) setCalOpen(false); };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  const d = new Date(selectedDate + "T12:00:00");
  const days = ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"];
  const months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  const dateDisplay = `${days[d.getDay()]}, ${d.getDate()} ${months[d.getMonth()]} ${d.getFullYear()}`;

  const topics = report?.topics || [];
  const visible = topics.filter((_, i) => !hidden.has(i));
  const cats = {}; visible.forEach(t => { cats[t.category] = (cats[t.category] || 0) + 1; });
  const actionCount = visible.reduce((s, t) => s + (t.action_items?.length || 0), 0);
  const photoCount = visible.reduce((s, t) => s + (t.photo_count || 0), 0);

  return (
    <div style={{ width: 380, minWidth: 380, borderRight: `1px solid ${T.border}`, display: "flex", flexDirection: "column", background: T.bg }}>
      {/* Date header */}
      <div style={{ padding: "14px 18px 12px", background: T.surface, borderBottom: `1px solid ${T.borderLight}` }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div style={{ position: "relative" }} ref={calRef}>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <button onClick={() => { const prev = new Date(d); prev.setDate(prev.getDate()-1); setSelectedDate(prev.toISOString().slice(0,10)); }}
                style={{ background: T.inputBg, border: `1px solid ${T.borderLight}`, borderRadius: 6, padding: "4px 10px", fontSize: 14, cursor: "pointer", color: T.textSecondary }}>‹</button>
              <div onClick={() => setCalOpen(!calOpen)} style={{ cursor: "pointer" }}>
                <div style={{ fontSize: 16, fontWeight: 700, color: T.text, display: "flex", alignItems: "center", gap: 4 }}>
                  {dateDisplay} <span style={{ fontSize: 10, color: T.textTertiary }}>▾</span>
                </div>
                {report && <div style={{ fontSize: 10, color: T.textTertiary }}>{report.user_name} · {report.device}</div>}
              </div>
              <button onClick={() => { const next = new Date(d); next.setDate(next.getDate()+1); setSelectedDate(next.toISOString().slice(0,10)); }}
                style={{ background: T.inputBg, border: `1px solid ${T.borderLight}`, borderRadius: 6, padding: "4px 10px", fontSize: 14, cursor: "pointer", color: T.textTertiary }}>›</button>
            </div>
            {calOpen && <CalendarPicker selectedDate={selectedDate} onSelect={setSelectedDate} onClose={() => setCalOpen(false)} dateData={dateData} />}
          </div>
        </div>

        {/* Stats */}
        {report && (
          <div style={{ display: "flex", gap: 5, marginTop: 10, flexWrap: "wrap" }}>
            <Pill label={`🎙 ${report._report_metadata?.recordings_processed || 0}`} />
            <Pill label={`📷 ${photoCount}`} />
            {Object.entries(cats).map(([k, v]) => {
              const m = topicColors[k]; return m ? <Pill key={k} label={`${m.icon} ${v} ${m.label}`} bg={m.bg} color={m.color} /> : null;
            })}
            {actionCount > 0 && <Pill label={`☐ ${actionCount} Actions`} bg={T.warningBg} color={T.warning} />}
          </div>
        )}
      </div>

      {/* Loading / Error / Empty */}
      {loading && (
        <div style={{ padding: 40, textAlign: "center" }}>
          <div style={{ fontSize: 24, opacity: 0.3, marginBottom: 8 }}>⏳</div>
          <div style={{ fontSize: 12, color: T.textTertiary }}>Loading report...</div>
        </div>
      )}
      {error && (
        <div style={{ padding: 20 }}>
          <div style={{ background: T.dangerBg, border: `1px solid ${T.danger}30`, borderRadius: 8, padding: "12px 16px", fontSize: 12, color: T.danger }}>{error}</div>
        </div>
      )}
      {!loading && !error && !report && (
        <div style={{ padding: 40, textAlign: "center" }}>
          <div style={{ fontSize: 32, opacity: 0.15 }}>📋</div>
          <div style={{ fontSize: 13, color: T.textTertiary, marginTop: 8 }}>No report for this date</div>
          <button onClick={onRegenerate} style={{ marginTop: 12, padding: "8px 16px", background: T.accent, color: "#fff", border: "none", borderRadius: 6, fontSize: 11, fontWeight: 600, cursor: "pointer" }}>Generate Report</button>
        </div>
      )}

      {/* AI Summary */}
      {report?.executive_summary && (
        <div style={{ padding: "10px 18px", borderBottom: `1px solid ${T.borderLight}`, background: T.surface }}>
          <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 5 }}>
            <div style={{ width: 3, height: 14, borderRadius: 2, background: T.accent }} />
            <span style={{ fontSize: 10, fontWeight: 700, color: T.accent, letterSpacing: 0.5 }}>AI SUMMARY</span>
          </div>
          <p style={{ fontSize: 11, color: T.textSecondary, lineHeight: 1.65, margin: 0 }}>{report.executive_summary}</p>
        </div>
      )}

      {/* Topic list */}
      {topics.length > 0 && (
        <div style={{ flex: 1, overflowY: "auto" }}>
          <div style={{ padding: "8px 18px 4px", fontSize: 10, fontWeight: 700, color: T.textTertiary, letterSpacing: 0.8 }}>
            TOPICS ({visible.length}){hidden.size > 0 && <span style={{ fontWeight: 400, marginLeft: 6 }}>· {hidden.size} hidden</span>}
          </div>
          {topics.map((topic, i) => {
            const isH = hidden.has(i);
            const isSel = selected === i;
            const cat = topicColors[topic.category];
            const hasHigh = topic.safety_flags?.some(f => f.risk_level === "high");
            return (
              <div key={i} onClick={() => !isH && setSelected(i)} style={{
                padding: "11px 18px", cursor: isH ? "default" : "pointer",
                background: isSel ? T.primaryFaded : "transparent",
                borderLeft: `3px solid ${isSel ? T.primary : "transparent"}`,
                borderBottom: `1px solid ${T.borderLight}`, opacity: isH ? 0.2 : 1,
              }}>
                <div style={{ display: "flex", gap: 8, alignItems: "flex-start" }}>
                  <span style={{ fontSize: 11, fontWeight: 700, color: T.text, fontFamily: T.mono, minWidth: 42, paddingTop: 1 }}>
                    {(topic.time_range || "").split("–")[0]?.trim()}
                  </span>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: 11, fontWeight: 600, color: T.text }}>{topic.topic_title}</div>
                    <div style={{ display: "flex", gap: 4, marginTop: 3, flexWrap: "wrap" }}>
                      {cat && <span style={{ fontSize: 8, fontWeight: 700, padding: "1px 6px", borderRadius: 8, background: cat.bg, color: cat.color }}>{cat.icon} {cat.label}</span>}
                      {hasHigh && <span style={{ fontSize: 8, fontWeight: 700, padding: "1px 6px", borderRadius: 8, background: T.dangerBg, color: T.danger }}>⚠ HIGH</span>}
                      {topic.photo_count > 0 && <span style={{ fontSize: 9, color: T.textTertiary }}>📷{topic.photo_count}</span>}
                    </div>
                    <div style={{ fontSize: 10, color: T.textSecondary, marginTop: 3, lineHeight: 1.4, overflow: "hidden", textOverflow: "ellipsis", display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical" }}>{topic.summary}</div>
                  </div>
                  <button onClick={(e) => { e.stopPropagation(); toggleHide(i); }}
                    style={{ background: "none", border: "none", fontSize: 10, color: T.textTertiary, cursor: "pointer", padding: 2, opacity: 0.5 }}>{isH ? "↩" : "✕"}</button>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* Bottom actions */}
      {report && (
        <div style={{ padding: "10px 18px", borderTop: `1px solid ${T.border}`, background: T.surface }}>
          <div style={{ display: "flex", gap: 6 }}>
            <button onClick={onRegenerate} style={{
              flex: 2, background: T.primary, color: "#fff", border: "none",
              fontSize: 11, fontWeight: 600, padding: "9px 0", borderRadius: 6, cursor: "pointer",
            }}>🔄 Regenerate Report</button>
          </div>
        </div>
      )}
    </div>
  );
}


/* ═══════════════════════════════════════════════ */
/* RIGHT PANEL: Topic Detail                      */
/* ═══════════════════════════════════════════════ */

function TopicDetail({ topic }) {
  const [showTranscript, setShowTranscript] = useState(false);

  if (!topic) {
    return (
      <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", background: T.bg }}>
        <div style={{ textAlign: "center", maxWidth: 320 }}>
          <div style={{ fontSize: 48, opacity: 0.12 }}>📋</div>
          <div style={{ fontSize: 15, color: T.textTertiary, fontWeight: 600, marginTop: 12 }}>Select a topic</div>
          <div style={{ fontSize: 12, color: T.textTertiary, marginTop: 6, lineHeight: 1.5 }}>Click any topic in the timeline to view the AI summary, decisions, actions, and photos</div>
        </div>
      </div>
    );
  }

  const cat = topicColors[topic.category];
  const highSafety = topic.safety_flags?.filter(f => f.risk_level === "high") || [];

  return (
    <div style={{ flex: 1, display: "flex", flexDirection: "column", background: T.bg, overflow: "hidden" }}>
      <div style={{ background: T.surface, padding: "14px 24px", borderBottom: `1px solid ${T.borderLight}`, flexShrink: 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <span style={{ fontSize: 16, fontWeight: 700, color: T.text }}>{topic.topic_title}</span>
          {cat && <span style={{ fontSize: 9, fontWeight: 700, padding: "2px 8px", borderRadius: 10, background: cat.bg, color: cat.color }}>{cat.icon} {cat.label}</span>}
          {highSafety.length > 0 && <span style={{ fontSize: 9, fontWeight: 700, padding: "2px 8px", borderRadius: 10, background: T.dangerBg, color: T.danger }}>⚠ HIGH RISK</span>}
        </div>
        <div style={{ fontSize: 11, color: T.textSecondary, marginTop: 4 }}>{topic.time_range}</div>
      </div>

      <div style={{ flex: 1, overflowY: "auto", padding: "18px 24px" }}>
        <Section title="SUMMARY">
          <Card><p style={{ fontSize: 13, color: T.text, lineHeight: 1.7, margin: 0 }}>{topic.summary}</p></Card>
        </Section>

        {highSafety.length > 0 && (
          <Section title="SAFETY ALERTS">
            {highSafety.map((sf, i) => (
              <Card key={i} borderColor={T.danger}>
                <div style={{ fontSize: 12, fontWeight: 600, color: T.danger, marginBottom: 4 }}>⚠ {sf.observation}</div>
                <div style={{ fontSize: 11, color: T.textSecondary }}>Recommended: {sf.recommended_action}</div>
              </Card>
            ))}
          </Section>
        )}

        {topic.key_decisions?.length > 0 && (
          <Section title="KEY DECISIONS">
            <Card>
              {topic.key_decisions.map((d, i) => (
                <div key={i} style={{ display: "flex", gap: 8, marginBottom: i < topic.key_decisions.length-1 ? 8 : 0 }}>
                  <span style={{ color: T.primary, fontSize: 11, marginTop: 1 }}>→</span>
                  <span style={{ fontSize: 12, color: T.text, lineHeight: 1.5 }}>{d}</span>
                </div>
              ))}
            </Card>
          </Section>
        )}

        {topic.action_items?.length > 0 && (
          <Section title={`ACTION ITEMS (${topic.action_items.length})`}>
            <Card>
              {topic.action_items.map((ai, i) => (
                <div key={i} style={{ display: "flex", gap: 10, padding: "8px 0", borderBottom: i < topic.action_items.length-1 ? `1px solid ${T.borderLight}` : "none" }}>
                  <div style={{ width: 18, height: 18, borderRadius: 4, flexShrink: 0, border: `2px solid ${T.border}`, cursor: "pointer" }} />
                  <div style={{ flex: 1 }}>
                    <div style={{ fontSize: 12, color: T.text }}>{ai.action}</div>
                    <div style={{ display: "flex", gap: 8, marginTop: 3, fontSize: 10, color: T.textTertiary, flexWrap: "wrap" }}>
                      <span>→ {ai.responsible}</span>
                      {ai.deadline && <span>· by {ai.deadline}</span>}
                      <span style={{ fontWeight: 700, color: ai.priority === "high" ? T.danger : T.textTertiary, textTransform: "uppercase" }}>{ai.priority}</span>
                    </div>
                  </div>
                </div>
              ))}
            </Card>
          </Section>
        )}

        {topic.photo_count > 0 && (
          <Section title={`PHOTOS (${topic.photo_count})`}>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(140px, 1fr))", gap: 10 }}>
              {(topic.related_photos || []).map((p, i) => (
                <div key={i} style={{ borderRadius: 8, overflow: "hidden", border: `1px solid ${T.borderLight}` }}>
                  <div style={{ height: 100, background: `linear-gradient(135deg, ${T.surfaceAlt}, ${T.accentLight})`, display: "flex", alignItems: "center", justifyContent: "center" }}>
                    <span style={{ fontSize: 24, opacity: 0.15 }}>📷</span>
                  </div>
                  <div style={{ padding: "6px 10px", background: T.surface }}>
                    <div style={{ fontSize: 10, fontWeight: 500, color: T.text, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{p}</div>
                  </div>
                </div>
              ))}
            </div>
          </Section>
        )}

        <Section title="RAW TRANSCRIPT">
          <button onClick={() => setShowTranscript(!showTranscript)} style={{
            width: "100%", padding: "10px 14px", background: T.surface, border: `1px solid ${T.borderLight}`, borderRadius: 8, cursor: "pointer",
            display: "flex", justifyContent: "space-between", alignItems: "center", fontSize: 11, color: T.textSecondary, fontFamily: T.font,
          }}>
            <span>{showTranscript ? "Hide raw transcript" : "View raw transcript"}</span>
            <span style={{ fontSize: 14, transform: showTranscript ? "rotate(180deg)" : "rotate(0)" }}>▾</span>
          </button>
          {showTranscript && (
            <div style={{ marginTop: 8, background: T.surface, borderRadius: 8, padding: "14px 16px", border: `1px solid ${T.borderLight}`, maxHeight: 300, overflowY: "auto" }}>
              <p style={{ fontSize: 12, color: T.textSecondary, lineHeight: 1.8, margin: 0 }}>
                <span style={{ color: T.textTertiary, fontFamily: T.mono, fontSize: 10 }}>[{(topic.time_range || "").split("–")[0]?.trim()}] </span>
                {topic.summary}
              </p>
            </div>
          )}
        </Section>
      </div>
    </div>
  );
}


/* ═══════════════════════════════════════════════ */
/* REPORTS TAB                                    */
/* ═══════════════════════════════════════════════ */

function ReportsTab({ token, user }) {
  const [reports, setReports] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api("/api/reports/history?limit=20", token)
      .then(data => setReports(data.reports || []))
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [token]);

  return (
    <div style={{ flex: 1, padding: 24, overflowY: "auto", background: T.bg }}>
      <div style={{ maxWidth: 740 }}>
        <div style={{ fontSize: 18, fontWeight: 700, color: T.text }}>Reports & History</div>
        <div style={{ fontSize: 12, color: T.textSecondary, marginBottom: 20 }}>{user.name}</div>

        <div style={{ fontSize: 11, fontWeight: 700, color: T.textTertiary, letterSpacing: 0.8, marginBottom: 8 }}>GENERATED REPORTS</div>
        {loading && <div style={{ fontSize: 12, color: T.textTertiary, padding: 20 }}>Loading...</div>}
        {reports.map((r, i) => (
          <div key={i} style={{ background: T.surface, borderRadius: 10, padding: "12px 16px", marginBottom: 6, border: `1px solid ${T.borderLight}`, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <div>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <span style={{ fontSize: 12, fontWeight: 600, color: T.text }}>
                  {r.type === "daily" ? "📋 Daily" : r.type === "weekly" ? "📊 Weekly" : "📈 Monthly"}
                </span>
                <span style={{ fontSize: 10, color: T.textTertiary }}>{r.date}</span>
              </div>
              <div style={{ fontSize: 10, color: T.textSecondary, marginTop: 3 }}>{r.generated_at?.split("T")[0]}</div>
            </div>
            <span style={{ fontSize: 10, color: T.textTertiary }}>{Math.round(r.size/1024)} KB</span>
          </div>
        ))}
        {!loading && reports.length === 0 && (
          <div style={{ padding: 20, textAlign: "center", color: T.textTertiary, fontSize: 12 }}>No reports generated yet</div>
        )}
      </div>
    </div>
  );
}


/* ═══════════════════════════════════════════════ */
/* MAIN APP                                       */
/* ═══════════════════════════════════════════════ */

export default function App() {
  const [auth, setAuth] = useState(null);
  const [user, setUser] = useState({ name: "", email: "" });
  const [tab, setTab] = useState("day");
  const [selected, setSelected] = useState(null);
  const [hidden, setHidden] = useState(new Set());
  const [selectedDate, setSelectedDate] = useState(() => {
    const now = new Date(); now.setHours(now.getHours() + 13); // NZDT approx
    now.setDate(now.getDate() - 1);
    return now.toISOString().slice(0, 10);
  });
  const [report, setReport] = useState(null);
  const [reportLoading, setReportLoading] = useState(false);
  const [reportError, setReportError] = useState("");
  const [dateData, setDateData] = useState({});
  const [regenMsg, setRegenMsg] = useState("");

  // Check stored auth
  useEffect(() => {
    const stored = sessionStorage.getItem("sitesync_auth");
    if (stored) {
      try {
        const parsed = JSON.parse(stored);
        const claims = parseJwt(parsed.idToken);
        if (claims.exp * 1000 > Date.now()) {
          setAuth(parsed);
          setUser({ name: claims.name || claims.email, email: claims.email });
        }
      } catch {}
    }
  }, []);

  const handleLogin = (tokens) => {
    const claims = parseJwt(tokens.idToken);
    setAuth(tokens);
    setUser({ name: claims.name || claims.email, email: claims.email });
    sessionStorage.setItem("sitesync_auth", JSON.stringify(tokens));
  };

  const handleLogout = () => {
    setAuth(null);
    setUser({ name: "", email: "" });
    sessionStorage.removeItem("sitesync_auth");
  };

  // Fetch report when date changes
  useEffect(() => {
    if (!auth) return;
    setReportLoading(true);
    setReportError("");
    setReport(null);
    setSelected(null);
    setHidden(new Set());

    api(`/api/timeline?date=${selectedDate}`, auth.idToken)
      .then(data => {
        if (data.topics) {
          setReport(data);
        } else if (data.available_users) {
          // Multiple users available, fetch first one
          return api(`/api/timeline?date=${selectedDate}&user=${data.available_users[0]}`, auth.idToken)
            .then(d => setReport(d.topics ? d : null));
        } else {
          setReport(null);
        }
      })
      .catch(err => {
        if (err.message === "UNAUTHORIZED") handleLogout();
        else setReportError(err.message);
      })
      .finally(() => setReportLoading(false));
  }, [selectedDate, auth]);

  // Fetch date index
  useEffect(() => {
    if (!auth) return;
    api("/api/dates?months=3", auth.idToken)
      .then(data => setDateData(data.dates || {}))
      .catch(() => {});
  }, [auth]);

  const toggleHide = (i) => {
    const next = new Set(hidden);
    next.has(i) ? next.delete(i) : next.add(i);
    setHidden(next);
    if (selected === i) setSelected(null);
  };

  const handleRegenerate = () => {
    if (!auth) return;
    setRegenMsg("Generating...");
    api("/api/reports/generate", auth.idToken, {
      method: "POST",
      body: JSON.stringify({ report_type: "daily", date: selectedDate, force: true }),
    }).then(() => {
      setRegenMsg("Report generation triggered. Refresh in 2-3 minutes.");
    }).catch(err => {
      setRegenMsg(`Error: ${err.message}`);
    });
  };

  if (!auth) return <LoginScreen onLogin={handleLogin} />;

  return (
    <>
      <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet" />
      <div style={{ height: "100vh", display: "flex", flexDirection: "column", fontFamily: T.font, background: T.bg, overflow: "hidden" }}>
        <NavBar tab={tab} setTab={setTab} user={user} onLogout={handleLogout} />
        {regenMsg && (
          <div style={{ background: T.warningBg, padding: "6px 20px", fontSize: 11, color: T.warning, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <span>{regenMsg}</span>
            <button onClick={() => setRegenMsg("")} style={{ background: "none", border: "none", color: T.warning, cursor: "pointer", fontSize: 12 }}>✕</button>
          </div>
        )}
        <div style={{ flex: 1, display: "flex", overflow: "hidden" }}>
          {tab === "day" ? (
            <>
              <LeftPanel report={report} loading={reportLoading} error={reportError}
                selected={selected} setSelected={setSelected} hidden={hidden} toggleHide={toggleHide}
                selectedDate={selectedDate} setSelectedDate={setSelectedDate} dateData={dateData}
                token={auth.idToken} onRegenerate={handleRegenerate} />
              <TopicDetail topic={selected != null && report?.topics ? report.topics[selected] : null} />
            </>
          ) : (
            <ReportsTab token={auth.idToken} user={user} />
          )}
        </div>
      </div>
    </>
  );
}
