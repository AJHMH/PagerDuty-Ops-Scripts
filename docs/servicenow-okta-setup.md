# Okta → ServiceNow authentication setup checklist

`pd-sn-sync` (with `SN_AUTH_METHOD=okta`, the default) gets an access token from Okta using
the OAuth 2.0 **client-credentials** grant. It then calls the ServiceNow Table API with
`Authorization: Bearer <token>`. ServiceNow must be configured to trust Okta-issued tokens for
inbound REST. Hand these two lists to your Okta and ServiceNow administrators.

> Exact menu names vary by Okta and ServiceNow release. Confirm the steps against your
> versions' documentation. Until this is done, `SN_AUTH_METHOD=sn-oauth` or `basic` works as
> a fallback.

## Okta administrator

1. **Create an app integration:** an *API Services* (machine-to-machine) app, e.g.
   `pd-ops-servicenow-sync`, with client authentication by client secret.
2. **Choose an authorization server.** Client credentials for custom resources require a
   *custom* authorization server, not the Org server. Record its **ID** (the `aus…` value)
   and its **issuer** `https://<okta-domain>/oauth2/<auth-server-id>`.
3. **Add a custom scope** on that server, e.g. `servicenow.table`.
4. **Add an access policy and rule** on that server that allows the *Client Credentials*
   grant for this app and the scope above. A short token lifetime (≤ 1 hour) is fine; the
   tool refreshes tokens.
5. **Decide which claim identifies the caller to ServiceNow.** For client credentials, `sub`
   is the client ID. Alternatively add a custom claim (e.g. `sn_user` = integration user name).
6. Provide these values to whoever runs the tool, through your secret store, not chat or email:

| Variable | Value |
|---|---|
| `OKTA_DOMAIN` | e.g. `dallas.okta.com` |
| `OKTA_AUTH_SERVER_ID` | the `aus…` ID |
| `OKTA_CLIENT_ID` | the app's client ID |
| `OKTA_CLIENT_SECRET` | the app's client secret |
| `OKTA_SCOPE` | e.g. `servicenow.table` |

## ServiceNow administrator (do `dallastx-dev` → `dallastx-test` → `dallastx`)

1. **Create the integration user,** e.g. `svc_pd_ops_sync`, non-interactive (web service
   access only), with the permissions in [servicenow.md](servicenow.md#integration-user-permissions).
2. **Register Okta as an external OIDC provider** for inbound tokens (System OAuth →
   Application Registry → *Configure an OIDC provider to verify ID tokens*):
   - OIDC metadata URL:
     `https://<okta-domain>/oauth2/<auth-server-id>/.well-known/openid-configuration`
   - Client ID: the Okta app's client ID
   - User claim: `sub` (or the custom claim from Okta step 5), mapped to a `sys_user` field
     (e.g. `user_name`) so the token resolves to the integration user.
3. **Allow bearer tokens on the Table API.** If your release uses REST API Access Policies,
   create or adjust a policy for `api/now/table` that permits OAuth/OIDC bearer
   authentication for the integration user.
4. **Test from a workstation** with read-only access before granting any write ACLs:

   ```bash
   export SN_AUTH_METHOD=okta OKTA_DOMAIN=... OKTA_AUTH_SERVER_ID=... \
          OKTA_CLIENT_ID=... OKTA_CLIENT_SECRET=... OKTA_SCOPE=...
   pd-sn-sync report --instance dallastx-dev
   ```

   An exit code of 3 means the token was rejected. Check the OIDC provider's user-claim mapping
   and the REST access policy.
5. **Grant field-level write ACLs** on the `x_pd_integration_*` columns only after the
   read-only report works.
