import {useEffect, useState} from "preact/hooks";

import {auditPanelAvailable, loadMutationLedger, type MutationRecord} from "./audit";
import type {AccessLevel} from "./capabilities";
import type {ApiRequester} from "./organizations";


export function AuditPanel({accessLevel, request}: {accessLevel: AccessLevel; request: ApiRequester}) {
  const [mutations, setMutations] = useState<MutationRecord[]>([]);
  const [status, setStatus] = useState("Loading mutation ledger…");

  useEffect(() => {
    let current = true;
    if (!auditPanelAvailable(accessLevel)) {
      setStatus("Owner or admin access is required to view the mutation ledger.");
      return () => { current = false; };
    }
    loadMutationLedger(request, accessLevel)
      .then((loaded) => {
        if (!current) return;
        setMutations(loaded);
        setStatus("");
      })
      .catch((error: unknown) => current && setStatus(error instanceof Error ? error.message : "Mutation ledger is unavailable"));
    return () => { current = false; };
  }, [accessLevel, request]);

  if (!auditPanelAvailable(accessLevel)) return <div id="auditPanel"><p>{status}</p></div>;

  return (
    <div id="auditPanel" class="parity-panel">
      <h3>Mutation Ledger</h3>
      <p>Configuration changes are recorded with actor role, target path, Before Hash, After Hash, and Details.</p>
      <div class="record-list">
        {mutations.length === 0 && <p>No mutations recorded. Settings changes will appear here after they are saved.</p>}
        {mutations.slice(0, 20).map((mutation) => (
          <article key={mutation.mutationId}>
            <strong>{mutation.action || "mutation"} · {mutation.status || "recorded"}</strong>
            <p>{mutation.actorRole || "unknown actor"} · {mutation.producedAt || "unknown time"}</p>
            <p>{mutation.targetPath || "unknown target"}</p>
            <dl class="audit-hashes">
              <div><dt>Before Hash</dt><dd>{mutation.beforeHash || "none"}</dd></div>
              <div><dt>After Hash</dt><dd>{mutation.afterHash || "none"}</dd></div>
              <div><dt>Changed</dt><dd>{mutation.changed ? "yes" : "no"}</dd></div>
            </dl>
            <details><summary>Details</summary><pre>{JSON.stringify(mutation.details || {}, null, 2)}</pre></details>
          </article>
        ))}
      </div>
      {status && <p role="status">{status}</p>}
    </div>
  );
}
