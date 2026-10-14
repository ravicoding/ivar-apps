import { test, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { mockClient } from 'aws-sdk-client-mock';
import { CognitoIdentityProviderClient, ListUsersCommand, AdminLinkProviderForUserCommand } from '@aws-sdk/client-cognito-identity-provider';

process.env.AWS_REGION = 'us-east-1';
process.env.AWS_ACCESS_KEY_ID = 'test';
process.env.AWS_SECRET_ACCESS_KEY = 'test';

const { handler } = await import('../presignup.mjs');
const mock = mockClient(CognitoIdentityProviderClient);

const ev = (over = {}, attrs = {}) => ({
  triggerSource: 'PreSignUp_ExternalProvider', userPoolId: 'us-east-1_pool', userName: 'Google_1234567890',
  request: { userAttributes: { email: 'me@example.com', email_verified: 'true', ...attrs } },
  response: {}, ...over
});

beforeEach(() => mock.reset());

test('a Google user with an existing password account is linked to it', async () => {
  mock.on(ListUsersCommand).resolves({ Users: [{ Username: 'native-user-id', UserStatus: 'CONFIRMED' }] });
  mock.on(AdminLinkProviderForUserCommand).resolves({});
  const out = await handler(ev());
  const link = mock.commandCalls(AdminLinkProviderForUserCommand)[0].args[0].input;
  assert.equal(link.DestinationUser.ProviderName, 'Cognito');
  assert.equal(link.DestinationUser.ProviderAttributeValue, 'native-user-id');
  assert.deepEqual(link.SourceUser, { ProviderName: 'Google', ProviderAttributeName: 'Cognito_Subject', ProviderAttributeValue: '1234567890' });
  assert.equal(out.response.autoConfirmUser, true);
  assert.equal(out.response.autoVerifyEmail, true);
});

test('email lookup is lower-cased and exact', async () => {
  mock.on(ListUsersCommand).resolves({ Users: [] });
  await handler(ev({}, { email: 'ME@Example.com' }));
  assert.equal(mock.commandCalls(ListUsersCommand)[0].args[0].input.Filter, 'email = "me@example.com"');
});

test('a new Google user just signs up, no link', async () => {
  mock.on(ListUsersCommand).resolves({ Users: [] });
  const out = await handler(ev());
  assert.equal(mock.commandCalls(AdminLinkProviderForUserCommand).length, 0);
  assert.equal(out.response.autoConfirmUser, true);
});

test('an earlier Google-only user is not used as the link target', async () => {
  mock.on(ListUsersCommand).resolves({ Users: [{ Username: 'Google_old', UserStatus: 'EXTERNAL_PROVIDER' }] });
  await handler(ev());
  assert.equal(mock.commandCalls(AdminLinkProviderForUserCommand).length, 0);
});

test('anyone else with a verified Google email is let in',async()=>{
  mock.on(ListUsersCommand).resolves({ Users: [] });
  const out = await handler(ev({}, { email: 'stranger@example.com' }));
  assert.equal(out.response.autoConfirmUser, true);
  assert.equal(mock.commandCalls(AdminLinkProviderForUserCommand).length, 0);
});

test('explicitly unverified email is rejected', async () => {
  await assert.rejects(handler(ev({}, { email_verified: 'false' })), /verified/);
});

test('missing email is rejected', async () => {
  const e = ev(); delete e.request.userAttributes.email;
  await assert.rejects(handler(e), /verified/);
});

test('only Google is accepted as a provider', async () => {
  await assert.rejects(handler(ev({ userName: 'Facebook_999' })), /Unsupported/);
  await assert.rejects(handler(ev({ userName: 'Google_' })), /Unsupported/);
});

test('admin-created and password sign-ups pass through untouched', async () => {
  const e = ev({ triggerSource: 'PreSignUp_AdminCreateUser' });
  assert.equal(await handler(e), e);
  assert.equal(mock.calls().length, 0);
});

test('an email with quotes cannot break out of the lookup filter', async () => {
  await assert.rejects(handler(ev({}, { email: 'a"b@example.com' })), /verified/);
});
