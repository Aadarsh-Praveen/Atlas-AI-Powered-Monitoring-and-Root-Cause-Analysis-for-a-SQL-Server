// Next.js configuration for the ShopLite storefront.
// Kept minimal on purpose: the storefront is plain pages that call the API
// from the browser, so it needs almost no framework configuration.

/** @type {import('next').NextConfig} */
const nextConfig = {
  // Development only: React renders components twice and runs effects twice
  // to surface side-effect bugs early. No effect in production builds.
  reactStrictMode: true,

  // Don't send "X-Powered-By: Next.js". Advertising the framework helps
  // nobody but someone looking for known vulnerabilities.
  poweredByHeader: false,
};

export default nextConfig;
