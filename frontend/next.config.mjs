/** @type {import('next').NextConfig} */
const nextConfig = {
  // Emit a self-contained production server at .next/standalone/server.js for the
  // multi-stage Docker runtime (docker/Dockerfile.frontend). No effect on
  // `next dev` / `next start`.
  output: "standalone",
};

export default nextConfig;
