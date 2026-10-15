// Publications API: AWS Lambda handler for an API Gateway HTTP API (payload format 2.0).
//
// Routes (all require a valid Cognito JWT from an allowed publisher except GET /health):
//   GET    /health
//   POST   /publications/upload-url   short-lived S3 upload form for one PDF
//   PUT    /publications/{slug}       publish or update one publication
//   DELETE /publications/{slug}       unpublish (the PDF, if any, is kept)
//
// Published files live in the website bucket:
//   publications/index.json     list shown on the homepage, newest first
//   publications/{slug}.json    one publication, rendered by publication.html
//   docs/publications/*.pdf     optional PDF attachments

import { randomUUID } from 'node:crypto';
import { S3Client, GetObjectCommand, PutObjectCommand, DeleteObjectCommand, HeadObjectCommand } from '@aws-sdk/client-s3';
import { CloudFrontClient, CreateInvalidationCommand } from '@aws-sdk/client-cloudfront';
import { createPresignedPost } from '@aws-sdk/s3-presigned-post';

const s3 = new S3Client({});
const cloudfront = new CloudFrontClient({});

const MAX_BODY_BYTES = 256 * 1024;
const MAX_TITLE = 200;
const MAX_AUTHORS = 200;
const MAX_SUMMARY = 600;
const MAX_TEXT = 200000;
const MAX_TAGS = 8;
const MAX_PDF_BYTES = 20 * 1024 * 1024;
const UPLOAD_EXPIRES_SECONDS = 300;
const INDEX_KEY = 'publications/index.json';
const PDF_PREFIX = 'docs/publications/';

const SLUG_RE = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const PDF_RE = /^docs\/publications\/[0-9a-f-]{36}\.pdf$/;

class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}
const bad = (code, message) => new ApiError(400, code, message);

const json = (statusCode, data) => ({
  statusCode,
  headers: { 'content-type': 'application/json', 'cache-control': 'no-store' },
  body: JSON.stringify(data)
});

const bucket = () => process.env.SITE_BUCKET;
const pubKey = slug => `publications/${slug}.json`;

/* ---------- Auth ---------- */

function publishers() {
  return new Set(String(process.env.PUBLISHER_EMAILS || '').split(',').map(e => e.trim().toLowerCase()).filter(Boolean));
}

function requirePublisher(event) {
  const claims = event?.requestContext?.authorizer?.jwt?.claims ?? {};
  const email = typeof claims.email === 'string' ? claims.email.toLowerCase() : '';
  const verified = claims.email_verified === true || claims.email_verified === 'true';
  if (!email) throw new ApiError(401, 'unauthorized', 'Sign in to publish');
  if (!verified || !publishers().has(email)) throw new ApiError(403, 'forbidden', 'This account is not allowed to publish');
  return email;
}

/* ---------- Input checks ---------- */

function parseBody(event) {
  if (!event.body) throw bad('invalid_body', 'A JSON request body is required');
  const raw = event.isBase64Encoded ? Buffer.from(event.body, 'base64').toString('utf8') : event.body;
  if (Buffer.byteLength(raw, 'utf8') > MAX_BODY_BYTES) throw new ApiError(413, 'too_large', 'Request body is too large');
  let parsed;
  try { parsed = JSON.parse(raw); } catch { throw bad('invalid_json', 'Request body must be valid JSON'); }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw bad('invalid_json', 'Request body must be a JSON object');
  return parsed;
}

function validSlug(v) {
  if (typeof v !== 'string' || v.length < 3 || v.length > 80 || !SLUG_RE.test(v)) {
    throw bad('invalid_slug', 'slug must be 3 to 80 lowercase letters, digits and single dashes');
  }
  return v;
}

function text(v, field, max, required) {
  if (v === undefined || v === null) v = '';
  if (typeof v !== 'string') throw bad(`invalid_${field}`, `${field} must be a string`);
  const t = v.replace(/\r\n?/g, '\n').trim();
  if (required && !t) throw bad(`invalid_${field}`, `${field} is required`);
  if (t.length > max) throw bad(`invalid_${field}`, `${field} can be at most ${max} characters`);
  return t;
}

function validTags(v) {
  if (v === undefined || v === null) return [];
  if (!Array.isArray(v) || v.length > MAX_TAGS) throw bad('invalid_tags', `tags must be a list of at most ${MAX_TAGS} words`);
  return [...new Set(v.map(t => text(t, 'tags', 40, true)))];
}

async function validPdf(v) {
  if (v === undefined || v === null || v === '') return null;
  if (typeof v !== 'string' || !PDF_RE.test(v)) throw bad('invalid_pdf', 'pdf must be a key returned by /publications/upload-url');
  try {
    await s3.send(new HeadObjectCommand({ Bucket: bucket(), Key: v }));
  } catch (err) {
    if (err?.name === 'NotFound' || err?.$metadata?.httpStatusCode === 404) throw bad('invalid_pdf', 'The PDF upload was not found. Upload it again.');
    throw err;
  }
  return v;
}

/* ---------- S3 helpers ---------- */

async function readJson(Key) {
  try {
    const res = await s3.send(new GetObjectCommand({ Bucket: bucket(), Key }));
    return { data: JSON.parse(await res.Body.transformToString()), etag: res.ETag };
  } catch (err) {
    if (err?.name === 'NoSuchKey' || err?.$metadata?.httpStatusCode === 404) return { data: null, etag: null };
    throw err;
  }
}

const putJson = (Key, data, cacheSeconds, condition = {}) => s3.send(new PutObjectCommand({
  Bucket: bucket(), Key, Body: JSON.stringify(data), ContentType: 'application/json; charset=utf-8',
  CacheControl: `public, max-age=${cacheSeconds}`, ...condition
}));

// Read, change and write the index with a conditional put, so two publishes at once never lose an entry.
async function updateIndex(change) {
  for (let attempt = 0; attempt < 4; attempt++) {
    const { data, etag } = await readJson(INDEX_KEY);
    const items = Array.isArray(data?.items) ? data.items : [];
    const next = change(items);
    next.sort((a, b) => String(b.publishedAt).localeCompare(String(a.publishedAt)));
    try {
      await putJson(INDEX_KEY, { updatedAt: new Date().toISOString(), items: next }, 60, etag ? { IfMatch: etag } : { IfNoneMatch: '*' });
      return;
    } catch (err) {
      const status = err?.$metadata?.httpStatusCode;
      if (status !== 412 && status !== 409) throw err;
    }
  }
  throw new ApiError(409, 'busy', 'The publication list changed while saving. Try again.');
}

async function invalidate(paths) {
  const id = process.env.DISTRIBUTION_ID;
  if (!id) return;
  try {
    await cloudfront.send(new CreateInvalidationCommand({
      DistributionId: id,
      InvalidationBatch: { CallerReference: `${Date.now()}-${randomUUID()}`, Paths: { Quantity: paths.length, Items: paths } }
    }));
  } catch (err) {
    // The files are saved; the cache expires on its own within minutes.
    console.error(JSON.stringify({ message: 'invalidation_failed', name: err?.name }));
  }
}

const summaryOf = p => ({
  slug: p.slug, title: p.title, authors: p.authors, summary: p.summary, tags: p.tags,
  pdf: p.pdf, publishedAt: p.publishedAt, updatedAt: p.updatedAt
});

/* ---------- Routes ---------- */

async function putPublication(email, event) {
  const slug = validSlug(event.pathParameters?.slug);
  const body = parseBody(event);
  const now = new Date().toISOString();
  const { data: existing } = await readJson(pubKey(slug));

  const pub = {
    slug,
    title: text(body.title, 'title', MAX_TITLE, true),
    authors: text(body.authors, 'authors', MAX_AUTHORS, false),
    summary: text(body.summary, 'summary', MAX_SUMMARY, false),
    body: text(body.body, 'body', MAX_TEXT, false),
    tags: validTags(body.tags),
    pdf: await validPdf(body.pdf),
    publishedAt: existing?.publishedAt ?? now,
    updatedAt: now,
    publishedBy: email
  };
  if (!pub.body && !pub.pdf) throw bad('empty_publication', 'A publication needs body text or a PDF');

  await putJson(pubKey(slug), pub, 300);
  await updateIndex(items => [...items.filter(i => i.slug !== slug), summaryOf(pub)]);
  await invalidate([`/${INDEX_KEY}`, `/${pubKey(slug)}`]);
  return json(200, { publication: pub, url: `/publication.html?p=${slug}`, created: !existing });
}

async function deletePublication(email, event) {
  const slug = validSlug(event.pathParameters?.slug);
  let found = false;
  await updateIndex(items => {
    found = items.some(i => i.slug === slug);
    return items.filter(i => i.slug !== slug);
  });
  await s3.send(new DeleteObjectCommand({ Bucket: bucket(), Key: pubKey(slug) }));
  await invalidate([`/${INDEX_KEY}`, `/${pubKey(slug)}`]);
  console.log(JSON.stringify({ message: 'unpublished', slug, by: email, found }));
  return json(200, { slug, deleted: true, found });
}

async function pdfUploadUrl() {
  const key = `${PDF_PREFIX}${randomUUID()}.pdf`;
  const post = await createPresignedPost(s3, {
    Bucket: bucket(),
    Key: key,
    Conditions: [['content-length-range', 1, MAX_PDF_BYTES], ['eq', '$Content-Type', 'application/pdf']],
    Fields: { 'Content-Type': 'application/pdf' },
    Expires: UPLOAD_EXPIRES_SECONDS
  });
  return json(200, { key, url: post.url, fields: post.fields, expiresIn: UPLOAD_EXPIRES_SECONDS, maxBytes: MAX_PDF_BYTES });
}

/* ---------- Entry point ---------- */

export const handler = async event => {
  try {
    if (event.routeKey === 'GET /health') return json(200, { ok: true });
    const email = requirePublisher(event);
    switch (event.routeKey) {
      case 'PUT /publications/{slug}': return await putPublication(email, event);
      case 'DELETE /publications/{slug}': return await deletePublication(email, event);
      case 'POST /publications/upload-url': return await pdfUploadUrl();
      default: throw new ApiError(404, 'not_found', 'Route not found');
    }
  } catch (err) {
    if (err instanceof ApiError) return json(err.status, { error: { code: err.code, message: err.message } });
    console.error(JSON.stringify({ message: 'unhandled_error', name: err?.name, route: event?.routeKey }));
    return json(500, { error: { code: 'internal', message: 'Something went wrong. Try again.' } });
  }
};
