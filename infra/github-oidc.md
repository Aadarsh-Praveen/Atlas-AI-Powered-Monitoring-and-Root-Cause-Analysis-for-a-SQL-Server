# GitHub Actions → Azure sign-in (OIDC), scoped to one VM

`.github/workflows/vm-schedule.yml` starts and deallocates `prod-sql-01`. It signs in to Azure with
**OpenID Connect (OIDC)** through a **user-assigned managed identity**, so **no Azure password, key,
or client secret is stored in GitHub**, and the identity can do exactly one thing: start, deallocate,
and read **that one VM**.

> **You run every command in this guide** (Claude Code never runs `az` or changes Azure). Run them on
> your Mac from the repo root, signed in with `az login` as an account that is **Owner** (or User
> Access Administrator) on the subscription. Creating a custom role and a role assignment needs that.

## How it works

```
GitHub Actions run (main branch)
   │  1. asks GitHub for a short-lived OIDC token:
   │     issuer  https://token.actions.githubusercontent.com
   │     subject repo:Aadarsh-Praveen@112577214/Atlas-AI-Powered-Monitoring-and-Root-Cause-Analysis-for-a-SQL-Server@1466078296:ref:refs/heads/main
   │             (owner and repository names WITH their numeric GitHub IDs; see step 3)
   ▼
Azure (Entra ID)
   │  2. finds a federated credential on the managed identity that trusts exactly
   │     that issuer + subject, and swaps the token for an Azure access token
   ▼
Azure Resource Manager
      3. the identity's only permission: role "ShopLite VM Operator" on prod-sql-01
         (read, instance view, start, deallocate)
```

- The GitHub token lasts minutes and only exists inside the run. Nothing long-lived to leak.
- Only runs on **`main`** match the subject. A pull request, another branch, or a fork gets no Azure
  access.
- A **managed identity** is an Azure resource in your subscription (here, in `rg-shoplite-prod`). It
  needs no Entra app registration and costs nothing.

## 1. Set some variables

```bash
RG=rg-shoplite-prod
VM=prod-sql-01
IDENTITY=id-github-shoplite
REPO=Aadarsh-Praveen/Atlas-AI-Powered-Monitoring-and-Root-Cause-Analysis-for-a-SQL-Server
SUB=$(az account show --query id -o tsv)
TENANT=$(az account show --query tenantId -o tsv)
echo "Subscription $SUB, tenant $TENANT"
```

Check that the subscription is the one holding `rg-shoplite-prod`.

## 2. Create the managed identity

```bash
az identity create --resource-group "$RG" --name "$IDENTITY" \
  --tags project=atlas env=prod app=shoplite

CLIENT_ID=$(az identity show -g "$RG" -n "$IDENTITY" --query clientId -o tsv)
PRINCIPAL_ID=$(az identity show -g "$RG" -n "$IDENTITY" --query principalId -o tsv)
echo "Client ID $CLIENT_ID"
```

It's created in the resource group's region (North Central US) with the same tags as everything else.

## 3. Trust this repo's `main` branch (federated credential)

Azure only exchanges a GitHub token whose **subject** matches the credential's subject **exactly**,
character for character.

**GitHub's subject includes numeric IDs.** For this repository, GitHub issues an ID-based subject:
the owner and repository names each followed by `@` and their numeric GitHub IDs:

```
repo:Aadarsh-Praveen@112577214/Atlas-AI-Powered-Monitoring-and-Root-Cause-Analysis-for-a-SQL-Server@1466078296:ref:refs/heads/main
```

The older, name-only form (`repo:Aadarsh-Praveen/Atlas-…-SQL-Server:ref:refs/heads/main`) does
**not** match it, and sign-in fails with `AADSTS700213` (see Troubleshooting). The IDs pin the
credential to *this* account and repository: if someone later created a different repository with the
same name, its tokens would carry different IDs and wouldn't be trusted.

```bash
SUBJECT="repo:Aadarsh-Praveen@112577214/Atlas-AI-Powered-Monitoring-and-Root-Cause-Analysis-for-a-SQL-Server@1466078296:ref:refs/heads/main"

az identity federated-credential create \
  --resource-group "$RG" --identity-name "$IDENTITY" \
  --name github-main \
  --issuer https://token.actions.githubusercontent.com \
  --subject "$SUBJECT" \
  --audiences api://AzureADTokenExchange
```

**Don't guess the subject; copy it from GitHub.** The surest source is the subject GitHub actually
presented, which a failed sign-in prints (see "Reading the presented subject" below). To check the two
IDs yourself, the GitHub CLI can show them (`.owner.id` is the account, `.id` the repository):

```bash
gh api repos/Aadarsh-Praveen/Atlas-AI-Powered-Monitoring-and-Root-Cause-Analysis-for-a-SQL-Server --jq '.owner.id, .id'
```

If the credential already exists with a different subject, update it in place:

```bash
az identity federated-credential update \
  --resource-group "$RG" --identity-name "$IDENTITY" --name github-main \
  --issuer https://token.actions.githubusercontent.com \
  --subject "$SUBJECT" \
  --audiences api://AzureADTokenExchange
```

Update the subject whenever the repository or the default branch is renamed or transferred, since the
names inside it change.

## 4. Create the custom role

No built-in role can only start and stop a VM (Virtual Machine Contributor can also delete, resize,
and run commands on it). `infra/roles/shoplite-vm-operator.json` allows exactly four operations:

| Permission | Why |
|---|---|
| `virtualMachines/read` | find the VM |
| `virtualMachines/instanceView/read` | report its power state |
| `virtualMachines/start/action` | morning start |
| `virtualMachines/deallocate/action` | evening stop (deallocate, which stops compute billing) |

Its "assignable scope" is the resource group (Azure doesn't allow a custom role's assignable scope to
be a single resource); the **assignment** in step 5 narrows it to the VM. Fill in your subscription
ID into a temporary copy, and create it:

```bash
sed "s/<subscription-id>/$SUB/" infra/roles/shoplite-vm-operator.json > /tmp/shoplite-vm-operator.json
az role definition create --role-definition @/tmp/shoplite-vm-operator.json
rm /tmp/shoplite-vm-operator.json
```

New custom roles can take a few minutes to become usable. If step 5 says the role doesn't exist,
wait and try again.

## 5. Assign the role on the VM only

```bash
VM_ID=$(az vm show -g "$RG" -n "$VM" --query id -o tsv)
az role assignment create \
  --assignee-object-id "$PRINCIPAL_ID" --assignee-principal-type ServicePrincipal \
  --role "ShopLite VM Operator" \
  --scope "$VM_ID"
```

The scope is the VM's resource ID, not the resource group or subscription.

## 6. Add the GitHub secrets

```bash
echo "AZURE_CLIENT_ID       = $CLIENT_ID"
echo "AZURE_TENANT_ID       = $TENANT"
echo "AZURE_SUBSCRIPTION_ID = $SUB"
```

In GitHub: **Settings → Secrets and variables → Actions → Secrets → New repository secret**, one
for each. They're identifiers, not passwords (on their own they grant nothing; only a token from a
`main` run can use them), but keeping them as secrets keeps them out of logs. Also make sure the
variable `API_URL` is set (`vm-schedule` uses it to report when the API is up).

## 7. Test it

1. **Actions → VM schedule → Run workflow**, `action: deallocate`. The run should be green, and its
   summary should say `prod-sql-01: deallocate → VM deallocated`.
2. Run it again with `action: start`. The summary shows `VM running` and, a few minutes later, how
   long the API took to answer `/health`.
3. If you disabled the workflow while waiting for this setup, **Enable workflow** now (see
   `.github/workflows/README.md`).

## 8. Check nothing else was granted

```bash
az role assignment list --assignee "$PRINCIPAL_ID" --all -o table
```

There should be **exactly one** row: role `ShopLite VM Operator`, scope ending in
`/virtualMachines/prod-sql-01`. Anything else, remove it.

## Troubleshooting

| Error in the workflow | Cause and fix |
|---|---|
| `AADSTS700213: No matching federated identity record found for presented assertion subject '…'` (older wording: `AADSTS70021`) | The token's subject doesn't match the credential's subject (step 3). Most often the credential has the **name-only** subject while GitHub presents the **ID-based** one (`owner@ID/repo@ID`). Read the presented subject from the log (below), compare it with `az identity federated-credential list -g $RG --identity-name $IDENTITY -o table`, and set the credential's subject to exactly that value (`az identity federated-credential update`, step 3). Also check that the run was on `main` (a manual run from another branch presents `ref:refs/heads/<that branch>`). |
| `AuthorizationFailed ... does not have authorization to perform action 'Microsoft.Compute/virtualMachines/start/action'` | The role assignment hasn't taken effect yet (wait 5–10 minutes), or it's on the wrong scope (step 8 shows it). |
| `No subscriptions found` during sign-in | The identity has no role yet (step 5), or it was just added. Wait a few minutes. |
| The secrets are empty in the run | They were added as variables instead of secrets, or under Environments instead of the repository. |

### Reading the presented subject from a failed run

When sign-in fails, the `azure/login` step prints Azure's error, which **quotes the subject GitHub
presented**:

1. Open the failed run: **Actions → VM schedule → the red run**.
2. Click the job (**vm**), then expand the step **Sign in to Azure (OIDC, no stored password)**.
3. Find the line with `AADSTS700213` (or `AADSTS70021`). It reads like:

   ```
   AADSTS700213: No matching federated identity record found for presented assertion subject
   'repo:Aadarsh-Praveen@112577214/Atlas-AI-Powered-Monitoring-and-Root-Cause-Analysis-for-a-SQL-Server@1466078296:ref:refs/heads/main'.
   ```

4. Copy the text **between the quotes**, exactly, without the quotes. That is the value for
   `--subject` in step 3. Check its last part is `ref:refs/heads/main`. If it names another branch,
   the run came from that branch; rerun from `main` instead of trusting the other branch.

The subject identifies the repository and branch; it isn't a secret, so it's fine that it appears in
the log.

## Undo

```bash
az role assignment delete --assignee "$PRINCIPAL_ID" --role "ShopLite VM Operator" --scope "$VM_ID"
az role definition delete --name "ShopLite VM Operator"
az identity delete -g "$RG" -n "$IDENTITY"
```

Deleting the resource group (`infra/README.md`, teardown) also removes the identity and the
assignment; the custom role definition remains until you delete it.
