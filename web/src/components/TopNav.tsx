import { KeyboardEvent, ReactNode, useEffect, useRef, useState } from "react";
import { Link, NavLink, useLocation } from "react-router-dom";
import { ExternalIcon } from "./icons";

/** Horizontal navigation: one button per section, each opening a dropdown of its pages.
 *  Hover (with a short intent delay) or click opens a menu; once one is open, moving to another
 *  section switches at once. Keyboard: arrow keys between sections and items, Esc closes. */

export interface NavItem {
  to: string;
  label: string;
  desc: string;
  /** keep the selected asset (?asset=) when following the link */
  keepAsset?: boolean;
  badge?: ReactNode;
  external?: boolean;
}

export interface NavSection {
  id: string;
  title: string;
  items: NavItem[];
  badge?: ReactNode;
}

const OPEN_DELAY = 110;
const CLOSE_DELAY = 220;

function isActive(pathname: string, to: string) {
  return to === "/" ? pathname === "/" : pathname === to || pathname.startsWith(`${to}/`);
}

function Chevron() {
  return (
    <svg className="chev" viewBox="0 0 12 12" aria-hidden="true">
      <path d="M3 4.5 6 7.5 9 4.5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function TopNav({ sections, assetQs }: { sections: NavSection[]; assetQs: string }) {
  const { pathname } = useLocation();
  const [open, setOpen] = useState<string | null>(null);
  const timer = useRef<number>();
  const bar = useRef<HTMLDivElement>(null);
  const buttons = useRef<Record<string, HTMLButtonElement | null>>({});

  const later = (fn: () => void, ms: number) => {
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(fn, ms);
  };

  useEffect(() => setOpen(null), [pathname]);
  useEffect(() => {
    if (!open) return;
    const outside = (e: PointerEvent) => {
      if (bar.current && !bar.current.contains(e.target as Node)) setOpen(null);
    };
    document.addEventListener("pointerdown", outside);
    return () => document.removeEventListener("pointerdown", outside);
  }, [open]);
  useEffect(() => () => window.clearTimeout(timer.current), []);

  const focusItem = (id: string, which: "first" | "last") => {
    requestAnimationFrame(() => {
      const items = bar.current?.querySelectorAll<HTMLElement>(`#menu-${id} [role="menuitem"]`);
      if (items?.length) (which === "first" ? items[0] : items[items.length - 1]).focus();
    });
  };

  const moveSection = (id: string, step: number, keepOpen: boolean) => {
    const i = sections.findIndex((s) => s.id === id);
    const next = sections[(i + step + sections.length) % sections.length];
    buttons.current[next.id]?.focus();
    if (keepOpen) {
      setOpen(next.id);
      focusItem(next.id, "first");
    }
  };

  const onButtonKey = (e: KeyboardEvent, id: string) => {
    if (e.key === "ArrowDown" || e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      setOpen(id);
      focusItem(id, "first");
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setOpen(id);
      focusItem(id, "last");
    } else if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      e.preventDefault();
      moveSection(id, e.key === "ArrowRight" ? 1 : -1, open !== null);
    } else if (e.key === "Escape") {
      setOpen(null);
    }
  };

  const onMenuKey = (e: KeyboardEvent, id: string) => {
    const items = Array.from(bar.current?.querySelectorAll<HTMLElement>(`#menu-${id} [role="menuitem"]`) ?? []);
    const i = items.indexOf(document.activeElement as HTMLElement);
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      items[(i + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length]?.focus();
    } else if (e.key === "Home" || e.key === "End") {
      e.preventDefault();
      (e.key === "Home" ? items[0] : items[items.length - 1])?.focus();
    } else if (e.key === "Escape") {
      e.preventDefault();
      setOpen(null);
      buttons.current[id]?.focus();
    } else if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      e.preventDefault();
      moveSection(id, e.key === "ArrowRight" ? 1 : -1, true);
    } else if (e.key === "Tab") {
      setOpen(null);
    }
  };

  return (
    <div className="topnav" ref={bar} role="menubar" aria-label="Main navigation" onMouseLeave={() => later(() => setOpen(null), CLOSE_DELAY)}>
      {sections.map((s) => {
        const here = s.items.some((it) => !it.external && isActive(pathname, it.to));
        const current = s.items.find((it) => !it.external && isActive(pathname, it.to));
        const isOpen = open === s.id;
        return (
          <div
            key={s.id}
            className={`navsec${isOpen ? " open" : ""}${here ? " here" : ""}`}
            onMouseEnter={() => later(() => setOpen(s.id), open ? 0 : OPEN_DELAY)}
          >
            <button
              ref={(el) => (buttons.current[s.id] = el)}
              type="button"
              className="navbtn"
              role="menuitem"
              aria-haspopup="menu"
              aria-expanded={isOpen}
              aria-controls={`menu-${s.id}`}
              onClick={() => setOpen(isOpen ? null : s.id)}
              onKeyDown={(e) => onButtonKey(e, s.id)}
            >
              <span className="navtitle">{s.title}</span>
              {current && <span className="navcurrent">{current.label}</span>}
              {s.badge}
              <Chevron />
            </button>
            <div id={`menu-${s.id}`} className="navmenu" role="menu" aria-label={s.title} hidden={!isOpen} onKeyDown={(e) => onMenuKey(e, s.id)}>
              {s.items.map((it) =>
                it.external ? (
                  <a key={it.to} href={it.to} target="_blank" rel="noopener" role="menuitem" className="navitem" tabIndex={-1}>
                    <span className="navlabel">
                      {it.label} <ExternalIcon size={13} />
                    </span>
                    <span className="navdesc">{it.desc}</span>
                  </a>
                ) : (
                  <NavLink
                    key={it.to}
                    to={it.to + (it.keepAsset === false ? "" : assetQs)}
                    end={it.to === "/"}
                    role="menuitem"
                    tabIndex={-1}
                    className={({ isActive: a }) => `navitem${a ? " active" : ""}`}
                    onClick={() => setOpen(null)}
                  >
                    <span className="navlabel">
                      {it.label}
                      {it.badge}
                    </span>
                    <span className="navdesc">{it.desc}</span>
                  </NavLink>
                ),
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

/** Phones: every section with its pages in one panel. */
export function MobileNav({ sections, assetQs, onNavigate }: { sections: NavSection[]; assetQs: string; onNavigate: () => void }) {
  return (
    <nav className="mobilenav" aria-label="Main navigation">
      {sections.map((s) => (
        <div key={s.id} className="mobsec">
          <h4>{s.title}</h4>
          {s.items.map((it) =>
            it.external ? (
              <a key={it.to} href={it.to} target="_blank" rel="noopener" className="navitem">
                <span className="navlabel">
                  {it.label} <ExternalIcon size={13} />
                </span>
              </a>
            ) : (
              <NavLink
                key={it.to}
                to={it.to + (it.keepAsset === false ? "" : assetQs)}
                end={it.to === "/"}
                className={({ isActive: a }) => `navitem${a ? " active" : ""}`}
                onClick={onNavigate}
              >
                <span className="navlabel">
                  {it.label}
                  {it.badge}
                </span>
              </NavLink>
            ),
          )}
        </div>
      ))}
    </nav>
  );
}

/** Signed-in user: initials, ID and role; account actions in a dropdown. */
export function UserMenu({
  name,
  login,
  role,
  kioskTo,
  onChangePassword,
  onSignOut,
}: {
  name: string;
  login: string;
  role: string;
  kioskTo: string;
  onChangePassword: () => void;
  onSignOut: () => void;
}) {
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const { pathname } = useLocation();
  useEffect(() => setOpen(false), [pathname]);
  useEffect(() => {
    if (!open) return;
    const outside = (e: PointerEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false);
    };
    const esc = (e: globalThis.KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);
  const initials = (name || login)
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((w) => w[0]!.toUpperCase())
    .join("");
  return (
    <div className={`usermenu${open ? " open" : ""}`} ref={box}>
      <button type="button" className="userbtn" aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen(!open)} title={`${login} · ${role}`}>
        <span className="avatar" aria-hidden="true">
          {initials}
        </span>
        <span className="userid">{login}</span>
        <Chevron />
      </button>
      <div className="navmenu right" role="menu" hidden={!open}>
        <div className="userhead">
          <b>{name || login}</b>
          <span className="muted mono">
            {login} · {role}
          </span>
        </div>
        <button type="button" role="menuitem" className="navitem" onClick={() => (setOpen(false), onChangePassword())}>
          <span className="navlabel">Change password</span>
        </button>
        <Link role="menuitem" className="navitem" to={kioskTo}>
          <span className="navlabel">
            Kiosk mode <ExternalIcon size={13} />
          </span>
          <span className="navdesc">Full-screen view for a control-room display</span>
        </Link>
        <button type="button" role="menuitem" className="navitem" onClick={onSignOut}>
          <span className="navlabel">Sign out</span>
        </button>
      </div>
    </div>
  );
}
