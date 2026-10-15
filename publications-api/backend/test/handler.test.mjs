import { test, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { mockClient } from 'aws-sdk-client-mock';
import { S3Client, GetObjectCommand, PutObjectCommand, DeleteObjectCommand, HeadObjectCommand } from '@aws-sdk/client-s3';
import { CloudFrontClient, CreateInvalidationCommand } from '@aws-sdk/client-cloudfront';

process.env.SITE_BUCKET = 'site';
process.env.DISTRIBUTION_ID = 'DIST';
process.env.PUBLISHER_EMAILS = 'Owner@Example.com, other@example.com';
const { handler } = await import('../handler.mjs');

const s3 = mockClient(S3Client);
const cf = mockClient(CloudFrontClient);
const notFound = () => Object.assign(new Error('nf'), { name: 'NoSuchKey', $metadata: { httpStatusCode: 404 } });
const body = d => ({ transformToString: async () => JSON.stringify(d) });

const ev = (routeKey, { slug, data, email = 'owner@example.com', verified = 'true' } = {}) => ({
  routeKey,
  pathParameters: slug ? { slug } : undefined,
  body: data === undefined ? undefined : JSON.stringify(data),
  requestContext: { authorizer: { jwt: { claims: email ? { email, email_verified: verified, sub: 'abc' } : {} } } }
});
const parse = r => ({ status: r.statusCode, data: JSON.parse(r.body) });

beforeEach(() => { s3.reset(); cf.reset(); cf.on(CreateInvalidationCommand).resolves({}); });

test('health needs no sign-in', async () => {
  assert.deepEqual(parse(await handler({ routeKey: 'GET /health' })), { status: 200, data: { ok: true } });
});

test('rejects users who are not publishers', async () => {
  assert.equal((await handler(ev('PUT /publications/{slug}', { slug: 'abc', data: {}, email: 'x@example.com' }))).statusCode, 403);
  assert.equal((await handler(ev('PUT /publications/{slug}', { slug: 'abc', data: {}, verified: 'false' }))).statusCode, 403);
  assert.equal((await handler(ev('PUT /publications/{slug}', { slug: 'abc', data: {}, email: '' }))).statusCode, 401);
});

test('publishes a new publication and adds it to the index', async () => {
  s3.on(GetObjectCommand).rejects(notFound());
  s3.on(PutObjectCommand).resolves({});
  const r = parse(await handler(ev('PUT /publications/{slug}', { slug: 'my-paper', data: { title: ' My paper ', body: 'Hello', tags: ['ai', 'ai'] } })));
  assert.equal(r.status, 200);
  assert.equal(r.data.created, true);
  assert.equal(r.data.publication.title, 'My paper');
  assert.deepEqual(r.data.publication.tags, ['ai']);
  const puts = s3.commandCalls(PutObjectCommand).map(c => c.args[0].input);
  assert.equal(puts[0].Key, 'publications/my-paper.json');
  assert.equal(puts[1].Key, 'publications/index.json');
  assert.equal(puts[1].IfNoneMatch, '*');
  assert.equal(JSON.parse(puts[1].Body).items[0].slug, 'my-paper');
  assert.equal(JSON.parse(puts[1].Body).items[0].body, undefined);
  assert.deepEqual(cf.commandCalls(CreateInvalidationCommand)[0].args[0].input.InvalidationBatch.Paths.Items,
    ['/publications/index.json', '/publications/my-paper.json']);
});

test('updating keeps the original publish date and retries on index conflicts', async () => {
  const old = { slug: 'my-paper', title: 'Old', publishedAt: '2026-01-01T00:00:00.000Z' };
  s3.on(GetObjectCommand, { Key: 'publications/my-paper.json' }).resolves({ Body: body(old), ETag: '"p"' });
  s3.on(GetObjectCommand, { Key: 'publications/index.json' }).resolves({ Body: body({ items: [old, { slug: 'b', publishedAt: '2026-05-01' }] }), ETag: '"i"' });
  s3.on(PutObjectCommand, { Key: 'publications/index.json' })
    .rejectsOnce(Object.assign(new Error('pf'), { $metadata: { httpStatusCode: 412 } }))
    .resolves({});
  s3.on(PutObjectCommand, { Key: 'publications/my-paper.json' }).resolves({});
  const r = parse(await handler(ev('PUT /publications/{slug}', { slug: 'my-paper', data: { title: 'New', body: 'x' } })));
  assert.equal(r.status, 200);
  assert.equal(r.data.created, false);
  assert.equal(r.data.publication.publishedAt, old.publishedAt);
  const idx = s3.commandCalls(PutObjectCommand).filter(c => c.args[0].input.Key === 'publications/index.json');
  assert.equal(idx.length, 2);
  assert.equal(idx[1].args[0].input.IfMatch, '"i"');
  const items = JSON.parse(idx[1].args[0].input.Body).items;
  assert.deepEqual(items.map(i => i.slug), ['b', 'my-paper']);
  assert.equal(items[1].title, 'New');
});

test('validates input', async () => {
  s3.on(GetObjectCommand).rejects(notFound());
  const bad = async (slug, data) => parse(await handler(ev('PUT /publications/{slug}', { slug, data }))).data.error.code;
  assert.equal(await bad('Bad Slug', { title: 'x', body: 'y' }), 'invalid_slug');
  assert.equal(await bad('ok-slug', { body: 'y' }), 'invalid_title');
  assert.equal(await bad('ok-slug', { title: 'x' }), 'empty_publication');
  assert.equal(await bad('ok-slug', { title: 'x', pdf: 'docs/other.pdf' }), 'invalid_pdf');
});

test('accepts an uploaded pdf only when it exists', async () => {
  s3.on(GetObjectCommand).rejects(notFound());
  s3.on(PutObjectCommand).resolves({});
  const key = 'docs/publications/0a1b2c3d-0000-4000-8000-000000000000.pdf';
  s3.on(HeadObjectCommand).rejectsOnce(Object.assign(new Error('nf'), { name: 'NotFound' })).resolves({});
  assert.equal(parse(await handler(ev('PUT /publications/{slug}', { slug: 'p-1', data: { title: 't', pdf: key } }))).data.error.code, 'invalid_pdf');
  assert.equal(parse(await handler(ev('PUT /publications/{slug}', { slug: 'p-1', data: { title: 't', pdf: key } }))).data.publication.pdf, key);
});

test('unpublish removes it from the index and deletes the file', async () => {
  s3.on(GetObjectCommand).resolves({ Body: body({ items: [{ slug: 'gone', publishedAt: '1' }, { slug: 'stay', publishedAt: '2' }] }), ETag: '"i"' });
  s3.on(PutObjectCommand).resolves({});
  s3.on(DeleteObjectCommand).resolves({});
  const r = parse(await handler(ev('DELETE /publications/{slug}', { slug: 'gone' })));
  assert.deepEqual(r.data, { slug: 'gone', deleted: true, found: true });
  assert.deepEqual(JSON.parse(s3.commandCalls(PutObjectCommand)[0].args[0].input.Body).items.map(i => i.slug), ['stay']);
  assert.equal(s3.commandCalls(DeleteObjectCommand)[0].args[0].input.Key, 'publications/gone.json');
});

test('upload url is limited to PDFs under the docs prefix', async () => {
  const r = parse(await handler(ev('POST /publications/upload-url', { data: {} })));
  assert.equal(r.status, 200);
  assert.match(r.data.key, /^docs\/publications\/[0-9a-f-]{36}\.pdf$/);
  assert.equal(r.data.fields['Content-Type'], 'application/pdf');
});
