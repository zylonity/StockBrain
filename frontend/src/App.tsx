import { Navigate, Route, Routes } from "react-router-dom";

import { AuthGate } from "./components/AuthGate";
import { Layout } from "./components/Layout";
import { Dashboard } from "./pages/Dashboard";
import { Discovery } from "./pages/Discovery";
import { EventDetail } from "./pages/EventDetail";
import { Events } from "./pages/Events";
import { Instruments } from "./pages/Instruments";
import { Logs } from "./pages/Logs";
import { Portfolio } from "./pages/Portfolio";
import { ProposalDetail, Proposals } from "./pages/Proposals";
import { Research, ResearchDetail } from "./pages/Research";
import { Settings } from "./pages/Settings";
import { SystemHealth } from "./pages/SystemHealth";

export function App() {
  return (
    <AuthGate>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<Dashboard />} />
          <Route path="events" element={<Events />} />
          <Route path="events/:eventId" element={<EventDetail />} />
          <Route path="discovery" element={<Discovery />} />
          <Route path="instruments" element={<Instruments />} />
          <Route path="portfolio" element={<Portfolio />} />
          <Route path="proposals" element={<Proposals />} />
          <Route path="proposals/:proposalId" element={<ProposalDetail />} />
          <Route path="research" element={<Research />} />
          <Route path="research/:runId" element={<ResearchDetail />} />
          <Route path="health" element={<SystemHealth />} />
          <Route path="logs" element={<Logs />} />
          <Route path="settings" element={<Settings />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </AuthGate>
  );
}
