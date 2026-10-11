// Daylog API: AWS Lambda handler for an API Gateway HTTP API (payload format 2.0).
//
// Routes (all require a valid Cognito JWT except GET /health):
//   GET    /health
//   PUT    /entries/{id}        create or update one entry (idempotent, last write wins)
//   GET    /entries?date=YYYY-MM-DD            entries for one day, newest first
//   GET    /entries?since=<ISO time>[&cursor=] everything changed since a time (for sync)
//   DELETE /entries/{id}        soft delete (kept 90 days so other devices can learn about it)
//   POST   /photos/upload-url   short-lived S3 upload form for one photo
//
// The user id always comes from the verified token (sub claim), never from the request.

import { randomUUID } from 'node:crypto';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient, PutCommand, UpdateCommand, QueryCommand } from '@aws-sdk/lib-dynamodb';
import { unmarshall } from '@aws-sdk/util-dynamodb';
import { S3Client, GetObjectCommand } from '@aws-sdk/client-s3';
import { createPresignedPost } from '@aws-sdk/s3-presigned-post';
import { getSignedUrl } from '@aws-sdk/s3-request-presigner';

const ddb = DynamoDBDocumentClient.from(new DynamoDBClient({}), { marshallOptions: { removeUndefinedValues: true } });
const s3 = new S3Client({});

const MAX_TEXT = 5000;
const MAX_IMAGES = 6;
const MAX_BODY_BYTES = 64 * 1024;
const MAX_PHOTO_BYTES = 8 * 1024 * 1024;
const CLOCK_SKEW_MS = 5 * 60 * 1000;
const TOMBSTONE_TTL_SECONDS = 90 * 24 * 60 * 60;
const UPLOAD_EXPIRES_SECONDS = 300;
const VIEW_EXPIRES_SECONDS = 900;
const IMAGE_TYPES = { 'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp' };

const ID_RE = /^[A-Za-z0-9_-]{8,64}$/;
const SUB_RE = /^[A-Za-z0-9-]{8,64}$/;
const CURSOR_KEYS = new Set(['PK', 'SK', 'GSI1PK', 'GSI1SK', 'GSI2PK', 'GSI2SK']);

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

const table = () => process.env.TABLE_NAME;
const bucket = () => process.env.PHOTO_BUCKET;
const userPk = sub => `USER#${sub}`;

/* ---------- Input checks ---------- */

function getSub(event) {
  const sub = event?.requestContext?.authorizer?.jwt?.claims?.sub;
  if (typeof sub !== 'string' || !SUB_RE.test(sub)) throw new ApiError(401, 'unauthorized', 'Sign in to use this API');
  return sub;
}

function validId(v) {
  if (typeof v !== 'string' || !ID_RE.test(v)) throw bad('invalid_id', 'id must be 8 to 64 letters, digits, dashes or underscores');
  return v;
}

function validDate(v, field) {
  if (typeof v !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(v)) throw bad('invalid_date', `${field} must look like 2026-10-10`);
  const d = new Date(`${v}T00:00:00Z`);
  if (Number.isNaN(d.getTime()) || d.toISOString().slice(0, 10) !== v) throw bad('invalid_date', `${field} is not a real date`);
  return v;
}

function validIso(v, field) {
  if (typeof v !== 'string') throw bad('invalid_time', `${field} must be an ISO 8601 timestamp`);
  const t = Date.parse(v);
  const year = new Date(t).getUTCFullYear();
  if (Number.isNaN(t) || year < 2000 || year > 2100) throw bad('invalid_time', `${field} must be an ISO 8601 timestamp`);
  return new Date(t).toISOString();
}

function parseBody(event) {
  if (!event.body) throw bad('invalid_body', 'A JSON request body is required');
  const raw = event.isBase64Encoded ? Buffer.from(event.body, 'base64').toString('utf8') : event.body;
  if (Buffer.byteLength(raw, 'utf8') > MAX_BODY_BYTES) throw new ApiError(413, 'too_large', 'Request body is too large. Upload photos with /photos/upload-url instead.');
  let parsed;
  try { parsed = JSON.parse(raw); } catch { throw bad('invalid_json', 'Request body must be valid JSON'); }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw bad('invalid_json', 'Request body must be a JSON object');
  return parsed;
}

function validImages(value, sub) {
  if (value === undefined) return [];
  if (!Array.isArray(value) || value.length > MAX_IMAGES) throw bad('invalid_images', `images must be a list of at most ${MAX_IMAGES} photo keys`);
  const prefix = `photos/${sub}/`;
  for (const key of value) {
    if (typeof key !== 'string' || !key.startsWith(prefix) || !/^[0-9a-f-]{36}\.(jpg|png|webp)$/.test(key.slice(prefix.length))) {
      throw bad('invalid_images', 'Each image must be a key returned by /photos/upload-url for your account');
    }
  }
  return [...new Set(value)];
}

/* ---------- Cursor (opaque, and only valid for the same user) ---------- */

const encodeCursor = key => (key ? Buffer.from(JSON.stringify(key)).toString('base64url') : null);

function decodeCursor(cursor, pk) {
  if (!cursor) return undefined;
  let key;
  try { key = JSON.parse(Buffer.from(cursor, 'base64url').toString('utf8')); } catch { throw bad('invalid_cursor', 'cursor is not valid'); }
  const valid = key && typeof key === 'object' && !Array.isArray(key) && key.PK === pk &&
    Object.entries(key).every(([k, v]) => CURSOR_KEYS.has(k) && typeof v === 'string') &&
    (key.GSI1PK === undefined || key.GSI1PK === pk) && (key.GSI2PK === undefined || key.GSI2PK === pk);
  if (!valid) throw bad('invalid_cursor', 'cursor is not valid');
  return key;
}

/* ---------- Shaping responses ---------- */

function publicEntry(item) {
  if (!item) return null;
  const { id, date, text, images, createdAt, updatedAt, deletedAt } = item;
  return { id, date, text: text ?? '', images: images ?? [], createdAt, updatedAt, ...(deletedAt ? { deletedAt } : {}) };
}

async function withUrls(entry) {
  if (entry.deletedAt || !entry.images.length) return { ...entry, imageUrls: [] };
  const imageUrls = await Promise.all(entry.images.map(Key =>
    getSignedUrl(s3, new GetObjectCommand({ Bucket: bucket(), Key }), { expiresIn: VIEW_EXPIRES_SECONDS })));
  return { ...entry, imageUrls };
}

/* ---------- Routes ---------- */

async function putEntry(sub, event) {
  const id = validId(event.pathParameters?.id);
  const body = parseBody(event);
  const date = validDate(body.date, 'date');
  if (body.text !== undefined && typeof body.text !== 'string') throw bad('invalid_text', 'text must be a string');
  const text = (body.text ?? '').trim();
  if (text.length > MAX_TEXT) throw bad('invalid_text', `text can be at most ${MAX_TEXT} characters`);
  const images = validImages(body.images, sub);
  if (!text && images.length === 0) throw bad('empty_entry', 'An entry needs text or at least one photo');

  const nowMs = Date.now();
  const createdAt = body.createdAt === undefined ? new Date(nowMs).toISOString() : validIso(body.createdAt, 'createdAt');
  let updatedAt = body.updatedAt === undefined ? new Date(nowMs).toISOString() : validIso(body.updatedAt, 'updatedAt');
  // A device with a wrong clock must not be able to win every future conflict.
  if (Date.parse(updatedAt) > nowMs + CLOCK_SKEW_MS) updatedAt = new Date(nowMs + CLOCK_SKEW_MS).toISOString();

  const pk = userPk(sub);
  const item = {
    PK: pk, SK: `ENTRY#${id}`,
    GSI1PK: pk, GSI1SK: `DATE#${date}#${createdAt}#${id}`,
    GSI2PK: pk, GSI2SK: `${updatedAt}#${id}`,
    entityType: 'ENTRY', id, date, text, images, createdAt, updatedAt
  };

  try {
    await ddb.send(new PutCommand({
      TableName: table(),
      Item: item,
      ConditionExpression: 'attribute_not_exists(PK) OR updatedAt < :u',
      ExpressionAttributeValues: { ':u': updatedAt },
      ReturnValuesOnConditionCheckFailure: 'ALL_OLD'
    }));
  } catch (err) {
    if (err?.name === 'ConditionalCheckFailedException') {
      // The server already has a newer version. Hand it back so the device can adopt it.
      let server = null;
      if (err.Item) { try { server = unmarshall(err.Item); } catch { server = err.Item; } }
      return json(200, { applied: false, entry: publicEntry(server) });
    }
    throw err;
  }
  return json(200, { applied: true, entry: publicEntry(item) });
}

async function listEntries(sub, event) {
  const q = event.queryStringParameters ?? {};
  const pk = userPk(sub);
  const parsedLimit = parseInt(q.limit ?? '50', 10);
  const limit = Math.min(Math.max(Number.isNaN(parsedLimit) ? 50 : parsedLimit, 1), 100);
  const cursor = decodeCursor(q.cursor, pk);

  let params;
  if (q.date !== undefined) {
    const date = validDate(q.date, 'date');
    params = {
      TableName: table(), IndexName: 'GSI1',
      KeyConditionExpression: 'GSI1PK = :pk AND begins_with(GSI1SK, :p)',
      ExpressionAttributeValues: { ':pk': pk, ':p': `DATE#${date}#` },
      ScanIndexForward: false, Limit: limit, ExclusiveStartKey: cursor
    };
    if (q.includeDeleted !== 'true') params.FilterExpression = 'attribute_not_exists(deletedAt)';
  } else if (q.since !== undefined) {
    const since = q.since === '' ? '0' : validIso(q.since, 'since');
    params = {
      TableName: table(), IndexName: 'GSI2',
      KeyConditionExpression: 'GSI2PK = :pk AND GSI2SK > :s',
      ExpressionAttributeValues: { ':pk': pk, ':s': since },
      ScanIndexForward: true, Limit: limit, ExclusiveStartKey: cursor
    };
  } else {
    throw bad('missing_filter', 'Provide date=YYYY-MM-DD or since=<ISO timestamp>');
  }

  const res = await ddb.send(new QueryCommand(params));
  const entries = await Promise.all((res.Items ?? []).map(item => withUrls(publicEntry(item))));
  return json(200, { entries, nextCursor: encodeCursor(res.LastEvaluatedKey) });
}

async function deleteEntry(sub, event) {
  const id = validId(event.pathParameters?.id);
  const now = new Date().toISOString();
  try {
    await ddb.send(new UpdateCommand({
      TableName: table(),
      Key: { PK: userPk(sub), SK: `ENTRY#${id}` },
      UpdateExpression: 'SET deletedAt = :n, updatedAt = :n, GSI2SK = :g, #ttl = :t',
      ConditionExpression: 'attribute_exists(PK)',
      ExpressionAttributeNames: { '#ttl': 'ttl' },
      ExpressionAttributeValues: { ':n': now, ':g': `${now}#${id}`, ':t': Math.floor(Date.now() / 1000) + TOMBSTONE_TTL_SECONDS }
    }));
  } catch (err) {
    if (err?.name === 'ConditionalCheckFailedException') throw new ApiError(404, 'not_found', 'Entry not found');
    throw err;
  }
  return json(200, { id, deletedAt: now });
}

async function photoUploadUrl(sub, event) {
  const body = parseBody(event);
  const ext = IMAGE_TYPES[body.contentType];
  if (!ext) throw bad('invalid_content_type', 'contentType must be image/jpeg, image/png or image/webp');
  const key = `photos/${sub}/${randomUUID()}.${ext}`;
  const post = await createPresignedPost(s3, {
    Bucket: bucket(),
    Key: key,
    Conditions: [['content-length-range', 1, MAX_PHOTO_BYTES], ['eq', '$Content-Type', body.contentType]],
    Fields: { 'Content-Type': body.contentType },
    Expires: UPLOAD_EXPIRES_SECONDS
  });
  return json(200, { key, url: post.url, fields: post.fields, expiresIn: UPLOAD_EXPIRES_SECONDS, maxBytes: MAX_PHOTO_BYTES });
}

/* ---------- Entry point ---------- */

export const handler = async event => {
  try {
    if (event.routeKey === 'GET /health') return json(200, { ok: true });
    const sub = getSub(event);
    switch (event.routeKey) {
      case 'PUT /entries/{id}': return await putEntry(sub, event);
      case 'GET /entries': return await listEntries(sub, event);
      case 'DELETE /entries/{id}': return await deleteEntry(sub, event);
      case 'POST /photos/upload-url': return await photoUploadUrl(sub, event);
      default: throw new ApiError(404, 'not_found', 'Route not found');
    }
  } catch (err) {
    if (err instanceof ApiError) return json(err.status, { error: { code: err.code, message: err.message } });
    // Log the error type only: entry text and photo keys are private.
    console.error(JSON.stringify({ message: 'unhandled_error', name: err?.name, route: event?.routeKey }));
    return json(500, { error: { code: 'internal', message: 'Something went wrong. Try again.' } });
  }
};
