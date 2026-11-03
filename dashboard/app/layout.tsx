import type { Metadata } from "next";
import localFont from "next/font/local";
import "./globals.css";

// Self-hosted latin subsets (OFL-1.1, see app/fonts/*/OFL.txt) so the build
// makes no network request for fonts. Keep the weight ranges narrow: the old
// Google Fonts setup declared only 500-700 and 400-500, so the browser clamps
// to them. Declaring the full axes would render body text and the KPI mono
// figures lighter or bolder than today.
const spaceGrotesk = localFont({
  src: "./fonts/space-grotesk/SpaceGrotesk-latin.woff2",
  weight: "500 700",
  variable: "--font-display",
});

const jetbrainsMono = localFont({
  src: "./fonts/jetbrains-mono/JetBrainsMono-latin.woff2",
  weight: "400 500",
  variable: "--font-jb-mono",
});

export const metadata: Metadata = {
  title: "Rainmaker",
  description: "Daily weather-market betting advisory",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className={`${spaceGrotesk.variable} ${jetbrainsMono.variable} h-full antialiased`}
    >
      <body className="min-h-full flex flex-col">{children}</body>
    </html>
  );
}