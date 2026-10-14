// Cognito "Pre sign-up" trigger. It runs when someone signs in with Google for the first time.
// Anyone with a Google account may sign in. This trigger:
//   1. Requires an email address that Google has not flagged as unverified.
//   2. If that email already has a password account, links the Google sign-in to it,
//      so both methods open the same Daylog data (same user id).
import { CognitoIdentityProviderClient, ListUsersCommand, AdminLinkProviderForUserCommand } from '@aws-sdk/client-cognito-identity-provider';

const cognito = new CognitoIdentityProviderClient({});
const SAFE_EMAIL = /^[^\s"\\,]+@[^\s"\\,]+$/;

export const handler = async event => {
  // Users you create yourself and password sign-ins are untouched.
  if (event.triggerSource !== 'PreSignUp_ExternalProvider') return event;

  const attrs = event.request?.userAttributes ?? {};
  const email = String(attrs.email ?? '').trim().toLowerCase();
  // Google sends a verified email for personal accounts. Refuse only if it says "not verified",
  // because linking by email is only safe when the email really belongs to that Google user.
  if (!SAFE_EMAIL.test(email) || attrs.email_verified === 'false') {
    throw new Error('This Google account has no verified email address.');
  }

  // Cognito names the Google user "Google_<google id>".
  const userName = String(event.userName ?? '');
  const cut = userName.indexOf('_');
  const provider = cut > 0 ? userName.slice(0, cut) : '';
  const providerId = cut > 0 ? userName.slice(cut + 1) : '';
  if (provider !== 'Google' || !providerId) throw new Error('Unsupported sign-in provider.');

  const found = await cognito.send(new ListUsersCommand({
    UserPoolId: event.userPoolId, Filter: `email = "${email}"`, Limit: 10
  }));
  const existing = (found.Users ?? []).find(u => u.UserStatus !== 'EXTERNAL_PROVIDER');
  if (existing) {
    await cognito.send(new AdminLinkProviderForUserCommand({
      UserPoolId: event.userPoolId,
      DestinationUser: { ProviderName: 'Cognito', ProviderAttributeValue: existing.Username },
      SourceUser: { ProviderName: 'Google', ProviderAttributeName: 'Cognito_Subject', ProviderAttributeValue: providerId }
    }));
  }

  event.response.autoConfirmUser = true;
  event.response.autoVerifyEmail = true;
  return event;
};
