import {render} from "preact";
import {useState} from "preact/hooks";

import "./styles.css";
import {WORK_MODES, type WorkMode} from "./work-mode";


function App() {
  const [workMode, setWorkMode] = useState<WorkMode>("normal");

  return (
    <main class="app-shell">
      <aside class="rail" aria-label="Steel Mission">
        <div class="mark" aria-hidden="true">SM</div>
        <div>
          <p class="eyebrow">Agent delivery plane</p>
          <strong>Steel Mission</strong>
        </div>
        <dl>
          <div><dt>Server</dt><dd>Not connected</dd></div>
          <div><dt>Authority</dt><dd>Advisory</dd></div>
        </dl>
      </aside>

      <section class="workspace">
        <header class="toolbar">
          <div>
            <label id="coordinatorModelLabel" for="coordinatorModel">Coordinator model</label>
            <select id="coordinatorModel" aria-describedby="coordinatorModelDescription" disabled>
              <option>Delivery Coordinator</option>
            </select>
            <p id="coordinatorModelDescription">Selects the model configuration that executes the Delivery Coordinator.</p>
          </div>

          <div class="mode-control">
            <span id="workModeLabel">Work mode</span>
            <div
              class="mode-switch"
              role="group"
              aria-labelledby="workModeLabel"
              aria-describedby="normalModeDescription domainCapabilityModeDescription"
            >
              {WORK_MODES.map((mode) => (
                <button
                  key={mode.id}
                  type="button"
                  aria-pressed={workMode === mode.id}
                  aria-describedby={mode.id === "normal" ? "normalModeDescription" : "domainCapabilityModeDescription"}
                  onClick={() => setWorkMode(mode.id)}
                >
                  {mode.label}
                </button>
              ))}
            </div>
            <p id="normalModeDescription" class="visually-hidden">{WORK_MODES[0].description}</p>
            <p id="domainCapabilityModeDescription" class="visually-hidden">{WORK_MODES[1].description}</p>
          </div>
        </header>

        <p
          id="domainCapabilityDefinition"
          class="definition"
          data-vocabulary-term="domain-capability"
        >
          Domain Capability: An assignable organizational role and workflow lens backed by governed knowledge.
        </p>

        <section class="empty-state" aria-labelledby="newConsoleTitle">
          <p class="eyebrow">Rebuilt console</p>
          <h1 id="newConsoleTitle">A typed shell, ready for parity work.</h1>
          <p>
            This non-default application proves the committed single-file build. Existing work,
            settings, and mission behavior remains on the current console until each surface reaches parity.
          </p>
        </section>
      </section>
    </main>
  );
}

const root = document.querySelector<HTMLElement>("#app");
if (!root) throw new Error("Steel Mission UI root is missing");
render(<App />, root);
