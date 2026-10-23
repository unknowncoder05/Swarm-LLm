import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  base: "/peerfoundry/",
  plugins: [react()],
  server: {
    port: 8082,
    // Proxy API calls to coordinator during local dev (bypasses CORS)
    proxy: {
      "/peerfoundry/api": {
        target: process.env.VITE_COORDINATOR_URL || "http://coordinator:8080",
        rewrite: (path) => path.replace(/^\/peerfoundry\/api/, ""),
        changeOrigin: true,
      },
    },
  },
});
