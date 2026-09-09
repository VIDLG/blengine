import { useEffect, useMemo, useRef, useState } from "react";
import * as stylex from "@stylexjs/stylex";
import { Badge, Button, Card, Collapsible, Divider, Grid, Heading, Stack, Text } from "@astryxdesign/core";
import { colorVars, radiusVars, spacingVars, typographyVars } from "@astryxdesign/core/theme/tokens.stylex";
import { createCommandRunner, errorMessage, initialSteps, initializeAdapter, runFlow } from "./flow.js";

const styles = stylex.create({
  root: {
    minHeight: "100vh",
    backgroundColor: colorVars["--color-background-body"],
    color: colorVars["--color-text-primary"],
    fontFamily: typographyVars["--font-family-body"],
    padding: spacingVars["--spacing-9"],
    "@media (max-width: 820px)": { padding: spacingVars["--spacing-5"] },
  },
  container: { width: "100%", maxWidth: 1120, marginInline: "auto" },
  header: {
    display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: spacingVars["--spacing-8"],
    "@media (max-width: 820px)": { display: "block" },
  },
  status: {
    minWidth: 220,
    borderInlineStartWidth: 2,
    borderInlineStartStyle: "solid",
    borderInlineStartColor: colorVars["--color-border-green"],
    borderRadius: radiusVars["--radius-inner"],
    paddingBlock: spacingVars["--spacing-3"],
    paddingInlineStart: spacingVars["--spacing-4"],
    "@media (max-width: 820px)": { marginBlockStart: spacingVars["--spacing-6"] },
  },
  actions: { marginBlock: spacingVars["--spacing-7"] },
  cardHeading: { display: "flex", justifyContent: "space-between", alignItems: "center", gap: spacingVars["--spacing-3"] },
  list: { width: "100%" },
  stepRow: {
    display: "grid", gridTemplateColumns: "30px minmax(0, 1fr) auto", alignItems: "center", gap: spacingVars["--spacing-2"],
    paddingBlock: spacingVars["--spacing-2"], borderBlockEndWidth: 1, borderBlockEndStyle: "solid", borderBlockEndColor: colorVars["--color-border"],
  },
  eventRow: {
    display: "grid", gridTemplateColumns: "minmax(0, 1.4fr) minmax(0, 1fr) auto", alignItems: "start", gap: spacingVars["--spacing-3"],
    paddingBlock: spacingVars["--spacing-2"], borderBlockEndWidth: 1, borderBlockEndStyle: "solid", borderBlockEndColor: colorVars["--color-border"],
  },
  eventList: { maxHeight: 510, overflowY: "auto" },
  eventDetail: {
    marginBlock: 0, padding: spacingVars["--spacing-3"], overflowX: "auto", borderRadius: radiusVars["--radius-inner"],
    backgroundColor: colorVars["--color-background-code"], color: colorVars["--color-text-primary"],
    fontFamily: typographyVars["--font-family-code"], fontSize: typographyVars["--font-size-supporting"], whiteSpace: "pre-wrap",
  },
  footer: { marginBlockStart: spacingVars["--spacing-4"] },
  code: { fontFamily: typographyVars["--font-family-code"], overflowWrap: "anywhere", minWidth: 0 },
});

function tauriApi() {
  const bridge = window.__TAURI__;
  return bridge ? { invoke: bridge.core.invoke, listen: bridge.event.listen } : null;
}

function statusVariant(status) {
  if (status.includes("failed")) return "error";
  if (status.endsWith("flow passed")) return "success";
  if (status === "disposed") return "warning";
  return "neutral";
}

export default function App() {
  const [status, setStatus] = useState("connecting event listener");
  const [steps, setSteps] = useState([]);
  const [events, setEvents] = useState([]);
  const [running, setRunning] = useState(false);
  const [disposed, setDisposed] = useState(false);
  const [initialization, setInitialization] = useState(null);
  const [flowBlocked, setFlowBlocked] = useState(false);
  const [capabilities, setCapabilities] = useState(null);
  const [listenerReady, setListenerReady] = useState(false);
  const busy = useRef(false);
  const disposing = useRef(false);
  const mounted = useRef(false);
  const api = useMemo(tauriApi, []);
  const runner = useMemo(() => api && createCommandRunner(api, {
    onCommand: (command) => {
      if (!mounted.current || disposing.current) throw new Error("example session has ended");
      setStatus(`running flow: ${command}`);
    },
  }), [api]);
  const apple = capabilities?.platform === "apple";
  const ready = listenerReady && capabilities !== null;

  useEffect(() => {
    mounted.current = true;
    if (!api) {
      setStatus("launch with Tauri host");
      return () => { mounted.current = false; };
    }
    let active = true;
    let stop;
    void (async () => {
      try {
        stop = await api.listen("blehub://event", ({ payload }) => {
          if (!active) return;
          setEvents((current) => [...current, payload]);
          runner.receive(payload);
        });
        if (!active) { stop?.(); return; }
        const value = await api.invoke("plugin:blehub|capabilities");
        if (!active) return;
        if (!value || typeof value.platform !== "string") throw new Error("invalid capabilities response");
        setCapabilities(value);
        setListenerReady(true);
        setStatus("ready");
      } catch (error) {
        if (active) setStatus(`setup failed: ${errorMessage(error)}`);
      }
    })();
    return () => {
      active = false;
      mounted.current = false;
      stop?.();
      runner.cancel();
    };
  }, [api, runner]);

  async function execute(initializeOnly = false) {
    if (!ready || busy.current || disposing.current || disposed || flowBlocked) return;
    busy.current = true;
    setRunning(true);
    setSteps([]);
    setEvents([]);
    const completed = [];
    const mark = (name, result, skipped) => {
      completed.push({ name, event_count: result.length, skipped });
      if (mounted.current && !disposing.current) setSteps([...completed]);
    };
    try {
      // Apple is initialized once via its explicit button. Demo starts with the
      // same initialize command; neither path manufactures initialization evidence.
      const evidence = initialization ?? await initializeAdapter(runner);
      if (!mounted.current || disposing.current) return;
      setInitialization(evidence);
      if (initializeOnly) {
        setStatus("CoreBluetooth initialized");
        return;
      }
      await runFlow(runner, capabilities, evidence, mark);
      if (mounted.current && !disposing.current) setStatus(apple ? "CoreBluetooth flow passed" : "basic flow passed");
    } catch (error) {
      if (mounted.current && !disposing.current) {
        setFlowBlocked(true);
        setStatus(`flow failed: ${errorMessage(error)}`);
      }
    } finally {
      busy.current = false;
      if (mounted.current) setRunning(false);
    }
  }

  async function disposeHub() {
    if (!ready || disposing.current || disposed) return;
    disposing.current = true;
    setFlowBlocked(true);
    runner.cancel(new Error("hub is being disposed"));
    setRunning(false);
    try {
      await api.invoke("plugin:blehub|close");
      if (mounted.current) {
        setDisposed(true);
        setInitialization(null);
        setStatus("disposed");
      }
    } catch (error) {
      disposing.current = false;
      if (mounted.current) setStatus(`dispose failed: ${errorMessage(error)}`);
    }
  }

  const displayedSteps = steps.length ? steps : initialSteps.map((name) => ({ name, event_count: 0 }));
  return (
    <main {...stylex.props(styles.root)}>
      <Stack gap={8} xstyle={styles.container}>
        <header {...stylex.props(styles.header)}>
          <Stack gap={2}>
            <Text type="supporting" color="accent">BLEHUB / TAURI</Text>
            <Heading level={1}>Central contract lab</Heading>
            <Text type="body" color="secondary" display="block">
              A visible React surface for the selected BleHub backend and event stream.
            </Text>
          </Stack>
          <Stack gap={2} xstyle={styles.status}>
            <Text type="supporting" color="secondary">Runtime status</Text>
            <Badge variant={statusVariant(status)} label={status} />
          </Stack>
        </header>
        <Stack gap={2} xstyle={styles.actions}>
          <Stack direction="horizontal" gap={2} wrap="wrap">
            {apple && initialization ? <Badge variant="success" label="CoreBluetooth ready" /> : (
              <Button variant="primary" onClick={() => execute(apple)} disabled={!ready || running || disposed || flowBlocked}>
                {running ? "Running..." : apple ? "Initialize CoreBluetooth" : "Run basic flow"}
              </Button>
            )}
            {apple && initialization && !disposed && (
              <Button variant="secondary" onClick={() => execute()} disabled={!ready || running || flowBlocked}>
                {flowBlocked ? "Flow failed — dispose to retry"
                  : steps.length === initialSteps.length ? "Run CoreBluetooth flow again" : "Run CoreBluetooth flow"}
              </Button>
            )}
            <Button variant="secondary" onClick={disposeHub} disabled={!ready || disposed}>Dispose hub</Button>
          </Stack>
          <Text type="supporting" color="secondary" display="block">
            {apple ? initialization
              ? "CoreBluetooth ready. Run the flow again after a clean disconnect; close the hub to end this session."
              : "Initialize CoreBluetooth once to begin. Close the hub to end this session."
              : capabilities?.platform === "test"
                ? "Demo flow only — not conformance evidence. Close the hub to end this session."
                : "Run the selected backend through the generic API. Close the hub to end this session."}
          </Text>
        </Stack>
        <Grid columns={{ minWidth: 320, repeat: "fit" }} gap={4}>
          <Card padding={4} elevation="low">
            <Stack gap={3}>
              <Stack direction="horizontal" justify="between" align="center" xstyle={styles.cardHeading}>
                <Heading level={2}>Contract coverage</Heading>
                <Badge variant={steps.length ? "success" : "neutral"} label={`${steps.length}/12`} />
              </Stack>
              <Divider />
              <Stack gap={0} xstyle={styles.list}>
                {displayedSteps.map((step) => (
                  <div {...stylex.props(styles.stepRow)} key={step.name}>
                    <Badge variant={step.skipped ? "warning" : steps.length ? "success" : "neutral"} label={step.skipped ? "N/A" : steps.length ? "OK" : "--"} />
                    <Text type="body">{step.name}</Text>
                    <Text type="supporting" color="secondary">{step.skipped ?? `${step.event_count} events`}</Text>
                  </div>
                ))}
              </Stack>
            </Stack>
          </Card>
          <Card padding={4} elevation="low">
            <Stack gap={3}>
              <Stack direction="horizontal" justify="between" align="center" xstyle={styles.cardHeading}>
                <Heading level={2}>Projected events</Heading>
                <Badge variant="info" label={events.length} />
              </Stack>
              <Divider />
              <Stack gap={0} xstyle={styles.eventList}>
                {events.length ? events.map((event, index) => {
                  const expandable = event.data != null || event.error != null;
                  const rowContent = (
                    <>
                      <Text type="code" xstyle={styles.code}>{event.kind}</Text>
                      <Text type="supporting" color="secondary">{event.detail ?? (event.error ? errorMessage(event.error) : event.kind === "command_completed" ? "command completed" : event.data?.state ?? "domain event")}</Text>
                      {expandable && <Text type="supporting" color="secondary">data</Text>}
                    </>
                  );
                  return expandable ? (
                    <Collapsible key={`${event.kind}-${index}`} trigger={<span {...stylex.props(styles.eventRow)}>{rowContent}</span>} defaultIsOpen={false}>
                      <pre {...stylex.props(styles.eventDetail)}>{JSON.stringify(event, null, 2)}</pre>
                    </Collapsible>
                  ) : <div key={`${event.kind}-${index}`} {...stylex.props(styles.eventRow)}>{rowContent}</div>;
                }) : <Text type="supporting" color="secondary" display="block">Run the flow to inspect domain and terminal events.</Text>}
              </Stack>
            </Stack>
          </Card>
        </Grid>
        <Text type="supporting" color="secondary" xstyle={styles.footer}>
          Backend platform: {capabilities?.platform ?? "pending"}. Event results are surfaced through the typed BleHub contract.
        </Text>
      </Stack>
    </main>
  );
}
