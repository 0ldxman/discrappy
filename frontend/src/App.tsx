import { NavLink, Route, Routes } from "react-router-dom";
import ScrapePage from "./pages/ScrapePage";
import SettingsPage from "./pages/SettingsPage";
import RunsPage from "./pages/RunsPage";
import RunTablePage from "./pages/RunTablePage";

export default function App() {
  return (
    <>
      <header className="topbar">
        <div className="brand"><span className="logo">💬→🗃</span> discrapp</div>
        <nav className="nav">
          <NavLink to="/" end>Скрэппинг</NavLink>
          <NavLink to="/runs">Прогоны</NavLink>
          <NavLink to="/settings">⚙ Настройки</NavLink>
        </nav>
      </header>
      <main>
        <Routes>
          <Route path="/" element={<ScrapePage />} />
          <Route path="/runs" element={<RunsPage />} />
          <Route path="/runs/:runId" element={<RunTablePage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<div className="card">Страница не найдена. <NavLink to="/">На главную</NavLink></div>} />
        </Routes>
      </main>
    </>
  );
}
