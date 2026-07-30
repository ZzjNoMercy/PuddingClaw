/** @type {import('next').NextConfig} */
const backendUrl = process.env.BACKEND_INTERNAL_URL || 'http://localhost:8888';

const nextConfig = {
  output: 'standalone',
  // Dev instances and production verification use distinct output directories.
  // scripts/next-dev.mjs scopes dev output by port; next-build.mjs owns
  // .next-build, so neither can invalidate another server's chunk graph.
  distDir: process.env.NEXT_DIST_DIR || '.next',
  // Next's gzip compressor buffers proxied text/event-stream responses until
  // the compression block is flushed. Browsers advertise Accept-Encoding by
  // default, so SSE appeared non-streaming even though curl without compression
  // received incremental chunks. Disable application-level compression; static
  // assets are already pre-compressed/cacheable and SSE must remain unbuffered.
  compress: false,
  async rewrites() {
    return [
      {
        source: '/api/:path*',
        destination: `${backendUrl}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
