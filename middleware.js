const SOCIAL_CRAWLERS = [
  'facebookexternalhit',
  'twitterbot',
  'linkedinbot',
  'whatsapp',
  'slackbot',
  'telegrambot',
  'discordbot',
  'pinterest',
];

export const config = {
  matcher: '/d/:slug*',
};

export default function middleware(request) {
  const ua = (request.headers.get('user-agent') || '').toLowerCase();
  const isCrawler = SOCIAL_CRAWLERS.some(bot => ua.includes(bot));

  if (!isCrawler) {
    return; // real users: vercel.json rewrite handles it
  }

  const url = new URL(request.url);
  const slug = url.pathname.replace('/d/', '');

  // Redirect crawlers to the native Vercel edge function
  return Response.redirect(`${url.origin}/api/og/${slug}`, 302);
}
