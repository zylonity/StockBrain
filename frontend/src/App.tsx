import { Navigate, Route, Routes } from "react-router-dom";

import { Layout } from "./components/Layout";
import { Dashboard } from "./pages/Dashboard";
import { Discovery } from "./pages/Discovery";
import { EventDetail } from "./pages/EventDetail";
import { Events } from "./pages/Events";
import { SystemHealth } from "./pages/SystemHealth";

export function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Dashboard />} />
        <Route path="events" element={<Events />} />
        <Route path="events/:eventId" element={<EventDetail />} />
        <Route path="discovery" element={<Discovery />} />
        <Route path="health" element={<SystemHealth />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
