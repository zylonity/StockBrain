import { Navigate, Route, Routes } from "react-router-dom";

import { Layout } from "./components/Layout";
import { Dashboard } from "./pages/Dashboard";
import { Discovery } from "./pages/Discovery";
import { EventDetail } from "./pages/EventDetail";
import { Instruments } from "./pages/Instruments";
import { ProposalDetail, Proposals } from "./pages/Proposals";
import { Events } from "./pages/Events";
import { Research, ResearchDetail } from "./pages/Research";
import { SystemHealth } from "./pages/SystemHealth";

export function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Dashboard />} />
        <Route path="events" element={<Events />} />
        <Route path="events/:eventId" element={<EventDetail />} />
        <Route path="discovery" element={<Discovery />} />
        <Route path="instruments" element={<Instruments />} />
        <Route path="proposals" element={<Proposals />} />
        <Route path="proposals/:proposalId" element={<ProposalDetail />} />
        <Route path="research" element={<Research />} />
        <Route path="research/:runId" element={<ResearchDetail />} />
        <Route path="health" element={<SystemHealth />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
