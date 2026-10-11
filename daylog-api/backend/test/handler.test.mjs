import { test, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { mockClient } from 'aws-sdk-client-mock';
import { DynamoDBDocumentClient, PutCommand, UpdateCommand, QueryCommand } from '@aws-sdk/lib-dynamodb';

process.env.TABLE_NAME = 'test-table';
process.env.PHOTO_BUCKET = 'test-bucket';
process.env.AWS_REGION = 'us-east-1';
process.env.AWS_ACCESS_KEY_ID = 'test';
process.env.AWS_SECRET_ACCESS_KEY = 'test';

const { handler } = await import('../handler.mjs');
const ddbMock = mockClient(DynamoDBDocumentClient);

const SUB = '11111111-2222-3333-4444-555555555555';
const OTHER = '99999999-8888-7777-6666-555555555555';
const PHOTO = `photos/${SUB}/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jpg`;
const ENTRY_ID = 'entry-0001';

const ev = (routeKey, { sub = SUB, body, pathParameters, query } = {}) => ({
  routeKey,
  requestContext: sub ? { authorizer: { jwt: { claims: { sub } } } } : {},
  body: body === undefined ? undefined : (typeof body === 'string' ? body : JSON.stringify(body)),
  pathParameters,
  queryStringParameters: query
});
const call = async (e) => { const r = await handler(e); return { status: r.statusCode, body: JSON.parse(r.body) }; };
const put = (body, opts = {}) => call(ev('PUT /entries/{id}', { pathParameters: { id: ENTRY_ID }, body, ...opts }));
const lastInput = Command => ddbMock.commandCalls(Command).at(-1).args[0].input;

const storedItem = (over = {}) => ({
  PK: `USER#${SUB}`, SK: `ENTRY#${ENTRY_ID}`, GSI1PK: `USER#${SUB}`, GSI1SK: 'DATE#2026-10-10#x', GSI2PK: `USER#${SUB}`, GSI2SK: 'y',
  entityType: 'ENTRY', id: ENTRY_ID, date: '2026-10-10', text: 'hello', images: [],
  createdAt: '2026-10-10T13:00:00.000Z', updatedAt: '2026-10-10T13:00:00.000Z', ...over
});

beforeEach(() => ddbMock.reset());

test('health check needs no sign-in', async () => {
  const r = await call({ routeKey: 'GET /health' });
  assert.equal(r.status, 200);
  assert.deepEqual(r.body, { ok: true });
});

test('rejects requests without a verified user', async () => {
  const none = await call(ev('GET /entries', { sub: null, query: { date: '2026-10-10' } }));
  assert.equal(none.status, 401);
  const weird = await call(ev('GET /entries', { sub: 'a#b', query: { date: '2026-10-10' } }));
  assert.equal(weird.status, 401);
  assert.equal(ddbMock.calls().length, 0);
});

test('unknown route returns 404', async () => {
  const r = await call(ev('PATCH /nothing'));
  assert.equal(r.status, 404);
  assert.equal(r.body.error.code, 'not_found');
});

/* ---------- PUT /entries/{id} ---------- */

test('saves an entry with keys derived from the token, not the body', async () => {
  ddbMock.on(PutCommand).resolves({});
  const r = await put({ date: '2026-10-10', text: '  Gym and groceries  ', userId: OTHER, PK: `USER#${OTHER}`, createdAt: '2026-10-10T13:00:00Z', images: [PHOTO] });
  assert.equal(r.status, 200);
  assert.equal(r.body.applied, true);
  assert.equal(r.body.entry.text, 'Gym and groceries');
  assert.equal(r.body.entry.PK, undefined);
  const item = lastInput(PutCommand).Item;
  assert.equal(item.PK, `USER#${SUB}`);
  assert.equal(item.SK, `ENTRY#${ENTRY_ID}`);
  assert.equal(item.GSI1SK, `DATE#2026-10-10#2026-10-10T13:00:00.000Z#${ENTRY_ID}`);
  assert.match(item.GSI2SK, new RegExp(`#${ENTRY_ID}$`));
  assert.deepEqual(item.images, [PHOTO]);
  assert.match(lastInput(PutCommand).ConditionExpression, /attribute_not_exists\(PK\) OR updatedAt < :u/);
  assert.equal(JSON.stringify(item).includes(OTHER), false);
});

test('accepts a photo-only entry', async () => {
  ddbMock.on(PutCommand).resolves({});
  const r = await put({ date: '2026-10-10', images: [PHOTO] });
  assert.equal(r.status, 200);
  assert.equal(lastInput(PutCommand).Item.text, '');
});

test('validates the entry', async () => {
  const cases = [
    [{ text: 'x' }, 'invalid_date'],
    [{ date: '2026-02-30', text: 'x' }, 'invalid_date'],
    [{ date: '10/10/2026', text: 'x' }, 'invalid_date'],
    [{ date: '2026-10-10' }, 'empty_entry'],
    [{ date: '2026-10-10', text: '   ' }, 'empty_entry'],
    [{ date: '2026-10-10', text: 5 }, 'invalid_text'],
    [{ date: '2026-10-10', text: 'a'.repeat(5001) }, 'invalid_text'],
    [{ date: '2026-10-10', text: 'x', createdAt: 'yesterday' }, 'invalid_time'],
    [{ date: '2026-10-10', text: 'x', updatedAt: '1970-01-01T00:00:00Z' }, 'invalid_time'],
    [{ date: '2026-10-10', text: 'x', images: 'nope' }, 'invalid_images'],
    [{ date: '2026-10-10', text: 'x', images: Array(7).fill(PHOTO) }, 'invalid_images'],
    [{ date: '2026-10-10', text: 'x', images: [`photos/${OTHER}/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jpg`] }, 'invalid_images'],
    [{ date: '2026-10-10', text: 'x', images: [`photos/${SUB}/../${OTHER}/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jpg`] }, 'invalid_images'],
    [{ date: '2026-10-10', text: 'x', images: ['https://evil.example/x.jpg'] }, 'invalid_images']
  ];
  for (const [body, code] of cases) {
    const r = await put(body);
    assert.equal(r.status, 400, JSON.stringify(body).slice(0, 80));
    assert.equal(r.body.error.code, code, JSON.stringify(body).slice(0, 80));
  }
  assert.equal((await call(ev('PUT /entries/{id}', { pathParameters: { id: 'x' }, body: { date: '2026-10-10', text: 'x' } }))).body.error.code, 'invalid_id');
  assert.equal(ddbMock.calls().length, 0);
});

test('rejects bad bodies', async () => {
  assert.equal((await put(undefined)).body.error.code, 'invalid_body');
  assert.equal((await put('{nope')).body.error.code, 'invalid_json');
  assert.equal((await put('[1,2]')).body.error.code, 'invalid_json');
  const big = await put(JSON.stringify({ date: '2026-10-10', text: 'x', pad: 'y'.repeat(70000) }));
  assert.equal(big.status, 413);
  const b64 = await call({ ...ev('PUT /entries/{id}', { pathParameters: { id: ENTRY_ID } }), isBase64Encoded: true, body: Buffer.from(JSON.stringify({ date: '2026-10-10', text: 'hi' })).toString('base64') });
  assert.equal(b64.status, 200);
});

test('returns the server copy when it is newer (last write wins)', async () => {
  const err = Object.assign(new Error('conditional'), {
    name: 'ConditionalCheckFailedException',
    Item: {
      PK: { S: `USER#${SUB}` }, SK: { S: `ENTRY#${ENTRY_ID}` }, id: { S: ENTRY_ID }, date: { S: '2026-10-10' },
      text: { S: 'server version' }, images: { L: [] }, createdAt: { S: '2026-10-10T13:00:00.000Z' }, updatedAt: { S: '2026-10-10T20:00:00.000Z' }
    }
  });
  ddbMock.on(PutCommand).rejects(err);
  const r = await put({ date: '2026-10-10', text: 'older edit', updatedAt: '2026-10-10T14:00:00Z' });
  assert.equal(r.status, 200);
  assert.equal(r.body.applied, false);
  assert.equal(r.body.entry.text, 'server version');
  assert.equal(r.body.entry.updatedAt, '2026-10-10T20:00:00.000Z');
  assert.equal(r.body.entry.PK, undefined);
});

test('clamps an updatedAt far in the future', async () => {
  ddbMock.on(PutCommand).resolves({});
  const before = Date.now();
  await put({ date: '2026-10-10', text: 'clock is wrong', updatedAt: '2099-01-01T00:00:00Z' });
  const sent = Date.parse(lastInput(PutCommand).Item.updatedAt);
  assert.ok(sent <= Date.now() + 5 * 60 * 1000 + 1000);
  assert.ok(sent >= before);
});

/* ---------- GET /entries ---------- */

test('lists one day newest first, without internal fields, with photo links', async () => {
  ddbMock.on(QueryCommand).resolves({
    Items: [storedItem({ images: [PHOTO] }), storedItem({ id: 'entry-0002', deletedAt: '2026-10-10T15:00:00.000Z', images: [PHOTO] })],
    LastEvaluatedKey: { PK: `USER#${SUB}`, SK: `ENTRY#${ENTRY_ID}`, GSI1PK: `USER#${SUB}`, GSI1SK: 'DATE#2026-10-10#x' }
  });
  const r = await call(ev('GET /entries', { query: { date: '2026-10-10' } }));
  assert.equal(r.status, 200);
  const input = lastInput(QueryCommand);
  assert.equal(input.IndexName, 'GSI1');
  assert.equal(input.ScanIndexForward, false);
  assert.equal(input.ExpressionAttributeValues[':pk'], `USER#${SUB}`);
  assert.equal(input.ExpressionAttributeValues[':p'], 'DATE#2026-10-10#');
  assert.equal(input.FilterExpression, 'attribute_not_exists(deletedAt)');
  assert.equal(r.body.entries[0].PK, undefined);
  assert.equal(r.body.entries[0].GSI1SK, undefined);
  assert.match(r.body.entries[0].imageUrls[0], /^https:\/\/test-bucket\.s3\.[^/]*amazonaws\.com\/photos\//);
  assert.deepEqual(r.body.entries[1].imageUrls, []);
  assert.ok(r.body.nextCursor);
});

test('can include deleted entries', async () => {
  ddbMock.on(QueryCommand).resolves({ Items: [] });
  await call(ev('GET /entries', { query: { date: '2026-10-10', includeDeleted: 'true' } }));
  assert.equal(lastInput(QueryCommand).FilterExpression, undefined);
});

test('sync pull uses the updated-time index', async () => {
  ddbMock.on(QueryCommand).resolves({ Items: [] });
  await call(ev('GET /entries', { query: { since: '2026-10-10T13:00:00Z' } }));
  let input = lastInput(QueryCommand);
  assert.equal(input.IndexName, 'GSI2');
  assert.equal(input.ScanIndexForward, true);
  assert.equal(input.ExpressionAttributeValues[':s'], '2026-10-10T13:00:00.000Z');
  assert.equal(input.FilterExpression, undefined);
  await call(ev('GET /entries', { query: { since: '' } }));
  assert.equal(lastInput(QueryCommand).ExpressionAttributeValues[':s'], '0');
});

test('cursor round trip works and cannot be used across users', async () => {
  const key = { PK: `USER#${SUB}`, SK: `ENTRY#${ENTRY_ID}`, GSI2PK: `USER#${SUB}`, GSI2SK: 'abc' };
  ddbMock.on(QueryCommand).resolves({ Items: [], LastEvaluatedKey: key });
  const first = await call(ev('GET /entries', { query: { since: '2026-10-10T13:00:00Z' } }));
  const again = await call(ev('GET /entries', { query: { since: '2026-10-10T13:00:00Z', cursor: first.body.nextCursor } }));
  assert.equal(again.status, 200);
  assert.deepEqual(lastInput(QueryCommand).ExclusiveStartKey, key);

  const stolen = await call(ev('GET /entries', { sub: OTHER, query: { since: '2026-10-10T13:00:00Z', cursor: first.body.nextCursor } }));
  assert.equal(stolen.status, 400);
  assert.equal(stolen.body.error.code, 'invalid_cursor');

  const forge = (k) => Buffer.from(JSON.stringify(k)).toString('base64url');
  for (const cursor of ['%%%', forge({ PK: `USER#${OTHER}` }), forge({ PK: `USER#${SUB}`, GSI2PK: `USER#${OTHER}` }), forge({ PK: `USER#${SUB}`, extra: 'x' }), forge([1])]) {
    const r = await call(ev('GET /entries', { query: { since: '2026-10-10T13:00:00Z', cursor } }));
    assert.equal(r.status, 400, cursor);
  }
});

test('list parameters are validated and limits are clamped', async () => {
  ddbMock.on(QueryCommand).resolves({ Items: [] });
  assert.equal((await call(ev('GET /entries'))).body.error.code, 'missing_filter');
  assert.equal((await call(ev('GET /entries', { query: { date: 'tomorrow' } }))).body.error.code, 'invalid_date');
  assert.equal((await call(ev('GET /entries', { query: { since: 'soon' } }))).body.error.code, 'invalid_time');
  await call(ev('GET /entries', { query: { date: '2026-10-10', limit: '1000' } }));
  assert.equal(lastInput(QueryCommand).Limit, 100);
  await call(ev('GET /entries', { query: { date: '2026-10-10', limit: 'abc' } }));
  assert.equal(lastInput(QueryCommand).Limit, 50);
  await call(ev('GET /entries', { query: { date: '2026-10-10', limit: '0' } }));
  assert.equal(lastInput(QueryCommand).Limit, 1);
});

test('each user only ever queries their own partition', async () => {
  ddbMock.on(QueryCommand).resolves({ Items: [] });
  await call(ev('GET /entries', { sub: SUB, query: { date: '2026-10-10' } }));
  await call(ev('GET /entries', { sub: OTHER, query: { date: '2026-10-10' } }));
  const pks = ddbMock.commandCalls(QueryCommand).map(c => c.args[0].input.ExpressionAttributeValues[':pk']);
  assert.deepEqual(pks, [`USER#${SUB}`, `USER#${OTHER}`]);
});

/* ---------- DELETE /entries/{id} ---------- */

test('soft deletes an entry and sets a 90 day expiry', async () => {
  ddbMock.on(UpdateCommand).resolves({});
  const r = await call(ev('DELETE /entries/{id}', { pathParameters: { id: ENTRY_ID } }));
  assert.equal(r.status, 200);
  assert.equal(r.body.id, ENTRY_ID);
  const input = lastInput(UpdateCommand);
  assert.deepEqual(input.Key, { PK: `USER#${SUB}`, SK: `ENTRY#${ENTRY_ID}` });
  assert.equal(input.ConditionExpression, 'attribute_exists(PK)');
  assert.deepEqual(input.ExpressionAttributeNames, { '#ttl': 'ttl' });
  const ttl = input.ExpressionAttributeValues[':t'];
  const days = (ttl - Date.now() / 1000) / 86400;
  assert.ok(days > 89.9 && days < 90.1);
  assert.equal(input.ExpressionAttributeValues[':g'], `${input.ExpressionAttributeValues[':n']}#${ENTRY_ID}`);
});

test('deleting a missing entry returns 404 and bad ids return 400', async () => {
  ddbMock.on(UpdateCommand).rejects(Object.assign(new Error('x'), { name: 'ConditionalCheckFailedException' }));
  assert.equal((await call(ev('DELETE /entries/{id}', { pathParameters: { id: ENTRY_ID } }))).status, 404);
  assert.equal((await call(ev('DELETE /entries/{id}', { pathParameters: { id: '../x' } }))).status, 400);
});

/* ---------- POST /photos/upload-url ---------- */

test('creates a size-limited upload form scoped to the user', async () => {
  const r = await call(ev('POST /photos/upload-url', { body: { contentType: 'image/jpeg' } }));
  assert.equal(r.status, 200);
  assert.match(r.body.key, new RegExp(`^photos/${SUB}/[0-9a-f-]{36}\\.jpg$`));
  assert.equal(r.body.fields.key, r.body.key);
  assert.equal(r.body.fields['Content-Type'], 'image/jpeg');
  assert.match(r.body.url, /test-bucket/);
  const policy = JSON.parse(Buffer.from(r.body.fields.Policy, 'base64').toString('utf8'));
  assert.ok(policy.conditions.some(c => Array.isArray(c) && c[0] === 'content-length-range' && c[2] === 8 * 1024 * 1024));
});

test('upload url rejects other file types', async () => {
  for (const contentType of ['image/gif', 'text/html', undefined, 5]) {
    const r = await call(ev('POST /photos/upload-url', { body: { contentType } }));
    assert.equal(r.status, 400);
    assert.equal(r.body.error.code, 'invalid_content_type');
  }
  assert.equal((await call(ev('POST /photos/upload-url'))).status, 400);
});

/* ---------- Errors ---------- */

test('unexpected failures return a generic 500 without leaking details', async () => {
  const original = console.error;
  const logged = [];
  console.error = (m) => logged.push(m);
  try {
    ddbMock.on(PutCommand).rejects(new Error('secret table name leaked'));
    const r = await put({ date: '2026-10-10', text: 'private diary text' });
    assert.equal(r.status, 500);
    assert.equal(r.body.error.code, 'internal');
    assert.equal(JSON.stringify(r.body).includes('secret'), false);
    assert.equal(logged.join('').includes('private diary text'), false);
    assert.equal(logged.join('').includes('secret'), false);
  } finally { console.error = original; }
});
