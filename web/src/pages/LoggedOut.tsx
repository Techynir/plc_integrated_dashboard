import { Link } from "react-router-dom";

export function LoggedOut() {
  return (
    <div className="login-wrap">
      <div className="card login-card" role="status">
        <div className="brand" style={{ padding: 0 }}>
          <img src="/favicon.svg" alt="" style={{ width: 30, height: 30 }} />
          <h1>PLC Dashboard</h1>
        </div>
        <h2>You have been signed out</h2>
        <p className="secondary" style={{ margin: 0 }}>
          You are signed out of the dashboard and the simulator. For security, close the browser if this is a shared computer.
        </p>
        <Link to="/login" className="btn primary" style={{ textAlign: "center" }}>
          Click here to sign in again
        </Link>
      </div>
    </div>
  );
}
