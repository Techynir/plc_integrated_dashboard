import { FormEvent, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "../api";
import { ErrorText, Modal } from "./ui";

/** Lets any signed-in user change their own password. Other sessions are signed out by the server. */
export function ChangePasswordModal({ onClose }: { onClose: () => void }) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [done, setDone] = useState(false);
  const mismatch = confirm.length > 0 && next !== confirm;
  const change = useMutation({
    mutationFn: () => api("/auth/password", { method: "POST", body: { current_password: current, new_password: next } }),
    onSuccess: () => setDone(true),
  });
  const submit = (e?: FormEvent) => {
    e?.preventDefault();
    if (next.length >= 8 && next === confirm) change.mutate();
  };

  if (done) {
    return (
      <Modal title="Password changed" onClose={onClose} footer={<button className="primary" onClick={onClose}>Done</button>}>
        <p style={{ margin: 0 }}>Your password has been changed. Any other devices or browsers signed in with your account have been signed out.</p>
      </Modal>
    );
  }
  return (
    <Modal
      title="Change password"
      onClose={onClose}
      footer={
        <>
          <button onClick={onClose}>Cancel</button>
          <button className="primary" onClick={() => submit()} disabled={change.isPending || next.length < 8 || next !== confirm || !current}>
            Change password
          </button>
        </>
      }
    >
      <form className="form-grid" onSubmit={submit}>
        <label className="field full">
          Current password
          <input type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} autoFocus required />
        </label>
        <label className="field">
          New password
          <input type="password" autoComplete="new-password" minLength={8} value={next} onChange={(e) => setNext(e.target.value)} required />
          <span className="hint">At least 8 characters</span>
        </label>
        <label className="field">
          Confirm new password
          <input type="password" autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} required />
          {mismatch && <span className="error-text">Passwords do not match</span>}
        </label>
        <button type="submit" hidden />
      </form>
      <ErrorText error={change.error} />
    </Modal>
  );
}
