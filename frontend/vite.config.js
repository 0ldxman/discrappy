import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
// Прод-сборка кладётся в dist/ и отдаётся FastAPI. В dev vite-сервер (5173)
// проксирует /api на uvicorn (8000).
export default defineConfig({
    plugins: [react()],
    server: {
        port: 5173,
        proxy: {
            "/api": "http://localhost:8000",
        },
    },
    build: {
        outDir: "dist",
        emptyOutDir: true,
    },
});
