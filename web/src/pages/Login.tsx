import { FormEvent, ReactNode, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api, User } from "../api";

function Icon({ children, size = 16 }: { children: ReactNode; size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {children}
    </svg>
  );
}

export function Login() {
  const qc = useQueryClient();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const user = await api<User>("/auth/login", { method: "POST", body: { email, password } });
      qc.setQueryData(["me"], user);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="nq-login">
      <video className="nq-login-video" autoPlay muted loop playsInline preload="auto" aria-hidden="true">
        <source src="/vector-node-motion.mp4" type="video/mp4" />
      </video>
      <div className="nq-login-overlay" aria-hidden="true" />
      <div className="nq-login-stage">
        <div className="nq-login-wrap">
          <aside className="nq-login-left">
            <div className="nq-orb nq-orb-1" />
            <div className="nq-orb nq-orb-2" />
            <div className="nq-login-brand">
              <img src="/Numerique.png" alt="Numerique" />
              <p>Manufacturing Intelligence Platform</p>
            </div>
            <div className="nq-login-hero">
              <h2>
                One platform.
                <br />
                Every production
                <br />
                insight.
              </h2>
              <p>Real-time plant overview, trends, alarms, and downtime — from the PLC as it arrives.</p>
              <div className="nq-stats">
                <div className="nq-stat">
                  <span className="nq-stat-icon dash">
                    <Icon>
                      <rect x="3" y="3" width="7" height="9" rx="1" />
                      <rect x="14" y="3" width="7" height="5" rx="1" />
                      <rect x="14" y="12" width="7" height="9" rx="1" />
                      <rect x="3" y="16" width="7" height="5" rx="1" />
                    </Icon>
                  </span>
                  <div>
                    <b>Plant</b>
                    <span>Overview</span>
                  </div>
                </div>
                <div className="nq-stat">
                  <span className="nq-stat-icon live">
                    <Icon>
                      <path d="M22 12h-4l-3 9L9 3l-3 9H2" />
                    </Icon>
                  </span>
                  <div>
                    <b className="with-dot">
                      <i className="nq-pulse" />
                      Live
                    </b>
                    <span>PLC data</span>
                  </div>
                </div>
                <div className="nq-stat">
                  <span className="nq-stat-icon alarm">
                    <Icon>
                      <path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9" />
                      <path d="M10.3 21a1.94 1.94 0 0 0 3.4 0" />
                    </Icon>
                  </span>
                  <div>
                    <b>Alarms</b>
                    <span>And events</span>
                  </div>
                </div>
              </div>
            </div>
            <p className="nq-login-foot">Industry 4.0 · PLC · Analytics</p>
          </aside>

          <section className="nq-login-right">
            <div className="nq-form-box">
              <img className="nq-mobile-logo" src="/Numerique2.png" alt="Numerique" />
              <div className="nq-login-copy">
                <div className="nq-eyebrow">Welcome back</div>
                <h1>Sign in to your workspace</h1>
                <p>Access live plant data, trends, and alarms.</p>
              </div>
              {error && (
                <div className="nq-error" role="alert">
                  <Icon size={15}>
                    <circle cx="12" cy="12" r="10" />
                    <path d="M12 8v4M12 16h.01" />
                  </Icon>
                  <span>{error}</span>
                </div>
              )}
              <form onSubmit={submit}>
                <label>
                  User ID
                  <span className="nq-field">
                    <i>
                      <Icon size={15}>
                        <path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2" />
                        <circle cx="12" cy="7" r="4" />
                      </Icon>
                    </i>
                    <input type="text" autoComplete="username" autoCapitalize="characters" spellCheck={false} placeholder="Enter your user ID" value={email} onChange={(e) => setEmail(e.target.value)} required autoFocus />
                  </span>
                </label>
                <label>
                  Password
                  <span className="nq-field">
                    <i>
                      <Icon size={15}>
                        <rect x="3" y="11" width="18" height="11" rx="2" />
                        <path d="M7 11V7a5 5 0 0 1 10 0v4" />
                      </Icon>
                    </i>
                    <input
                      type={showPassword ? "text" : "password"}
                      autoComplete="current-password"
                      placeholder="Enter your password"
                      value={password}
                      onChange={(e) => setPassword(e.target.value)}
                      required
                    />
                    <button type="button" className="nq-eye" onClick={() => setShowPassword((v) => !v)} aria-label={showPassword ? "Hide password" : "Show password"}>
                      {showPassword ? (
                        <Icon size={15}>
                          <path d="M9.88 9.88a3 3 0 1 0 4.24 4.24" />
                          <path d="M10.73 5.08A10.43 10.43 0 0 1 12 5c7 0 10 7 10 7a13.16 13.16 0 0 1-1.67 2.68" />
                          <path d="M6.61 6.61A13.526 13.526 0 0 0 2 12s3 7 10 7a9.74 9.74 0 0 0 5.39-1.61" />
                          <line x1="2" x2="22" y1="2" y2="22" />
                        </Icon>
                      ) : (
                        <Icon size={15}>
                          <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7" />
                          <circle cx="12" cy="12" r="3" />
                        </Icon>
                      )}
                    </button>
                  </span>
                </label>
                <button className="nq-signin" type="submit" disabled={busy}>
                  {busy ? (
                    <span className="nq-spinner" />
                  ) : (
                    <>
                      <Icon size={17}>
                        <path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4" />
                        <path d="m10 17 5-5-5-5" />
                        <path d="M15 12H3" />
                      </Icon>
                      Sign in
                    </>
                  )}
                </button>
              </form>
              <div className="nq-divider">
                <span />
                Available views
                <span />
              </div>
              <div className="nq-tags">
                <span>Plant overview</span>
                <span>Trends</span>
                <span>Alarms</span>
                <span>Performance</span>
                <span>Quality</span>
              </div>
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}
