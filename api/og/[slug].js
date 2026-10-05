export const config = {
  runtime: 'edge',
};

export default async function handler(request) {
  const url = new URL(request.url);
  const slug = url.pathname.split('/').pop();

  if (!slug) {
    return new Response('Not found', { status: 404 });
  }

  const supabaseUrl = 'https://qolksrytidvxarrlygyy.supabase.co';
  const serviceKey = process.env.SUPABASE_SERVICE_ROLE_KEY;

  let product;
  try {
    const res = await fetch(
      `${supabaseUrl}/rest/v1/products?slug=eq.${encodeURIComponent(slug)}&select=title,price,original_price,affiliate_link,slug&limit=1`,
      {
        headers: {
          'apikey': serviceKey,
          'Authorization': `Bearer ${serviceKey}`,
          'Content-Type': 'application/json',
        },
      }
    );
    const data = await res.json();
    product = data?.[0];
  } catch (e) {
    return new Response('Error fetching product', { status: 500 });
  }

  if (!product) {
    return new Response('Deal not found', { status: 404 });
  }

  const imageUrl = `${supabaseUrl}/storage/v1/object/public/social-images/og/${product.slug}.png`;
  const title = product.title;
  const description = `$${Number(product.price).toFixed(2)}, was $${Number(product.original_price).toFixed(2)}.`;
  const destination = product.affiliate_link;

  const html = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${title}</title>
<link rel="canonical" href="${destination}">
<meta name="description" content="${description}">
<meta property="og:type" content="product">
<meta property="og:site_name" content="MistryDeals">
<meta property="og:url" content="${destination}">
<meta property="og:title" content="${title}">
<meta property="og:description" content="${description}">
<meta property="og:image" content="${imageUrl}">
<meta property="og:image:secure_url" content="${imageUrl}">
<meta property="og:image:type" content="image/png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:image:alt" content="${title}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="${title}">
<meta name="twitter:description" content="${description}">
<meta name="twitter:image" content="${imageUrl}">
</head>
<body>
<p><a href="${destination}">${title} — ${description}</a></p>
</body>
</html>`;

  return new Response(html, {
    status: 200,
    headers: {
      'Content-Type': 'text/html; charset=utf-8',
      'Cache-Control': 'no-store',
    },
  });
}
