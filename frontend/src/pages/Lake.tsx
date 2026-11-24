import { Embedded, PageHeader, useKept, useNav } from "../ui";
import KnowledgeBases from "./KnowledgeBases";
import Sheets from "./lake/Sheets";
import Topics from "./lake/Topics";
import { Offline } from "./Library";
import Vault from "./Vault";

const TABS = [["topics", "Topics"], ["sheets", "Sheets"], ["collections", "Collections"], ["libraries", "Libraries"], ["vault", "Vault"]] as const;

/** Everything in the lake, like Docker Desktop's Containers / Images / Volumes. */
export default function Lake() {
  const { params } = useNav();
  const [tab, setTab] = useKept("lake:tab", params.tab ?? "sheets");
  const current = params.new || params.sheet ? "sheets" : params.tab ?? tab;
  return (
    <div>
      <PageHeader title="Lake">
        <div className="seg overflow-x-auto" role="tablist" aria-label="Lake">
          {TABS.map(([id, label]) => <button key={id} role="tab" aria-selected={current === id} onClick={() => setTab(id)}>{label}</button>)}
        </div>
      </PageHeader>
      <Embedded.Provider value={true}>
        {current === "topics" && <Topics />}
        {current === "sheets" && <Sheets />}
        {current === "collections" && <KnowledgeBases />}
        {current === "libraries" && <Offline />}
        {current === "vault" && <Vault />}
      </Embedded.Provider>
    </div>
  );
}
