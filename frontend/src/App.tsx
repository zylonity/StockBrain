import { Navigate, Route, Routes } from "react-router-dom";

import { Layout } from "./components/Layout";
import { Dashboard } from "./pages/Dashboard";
import { SystemHealth } from "./pages/SystemHealth";

export function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Dashboard />} />
        <Route path="health" element={<SystemHealth />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
