import { Embedded, PageHeader, useKept, useNav } from "../ui";
import KnowledgeBases from "./KnowledgeBases";
import { Query, Views } from "./lake/Query";
import Sheets from "./lake/Sheets";
import Topics from "./lake/Topics";
import { Offline } from "./Library";
import Vault from "./Vault";

/** One page per kind of data. Each reuses the component that used to live in a Lake tab. */
function Page({ title, children }: { title: string; children: React.ReactNode }) {
  return <div><PageHeader title={title} /><Embedded.Provider value={true}>{children}</Embedded.Provider></div>;
}

export const TopicsPage = () => <Page title="Topics"><Topics /></Page>;
export const CollectionsPage = () => <Page title="Collections"><KnowledgeBases /></Page>;
export const LibrariesPage = () => <Page title="Libraries"><Offline /></Page>;
export const VaultPage = () => <Page title="Vault"><Vault /></Page>;

const TABLE_TABS = [["tables", "Tables"], ["query", "Query"], ["views", "Saved views"]] as const;

export function TablesPage() {
  const { params } = useNav();
  const [tab, setTab] = useKept("tables:tab", "tables");
  const current = params.sheet || params.new ? "tables" : tab;
  return (
    <div>
      <PageHeader title="Tables">
        <div className="seg" role="tablist" aria-label="Tables">
          {TABLE_TABS.map(([id, label]) => <button key={id} role="tab" aria-selected={current === id} onClick={() => setTab(id)}>{label}</button>)}
        </div>
      </PageHeader>
      <Embedded.Provider value={true}>
        {current === "tables" && <Sheets />}
        {current === "query" && <Query />}
        {current === "views" && <Views />}
      </Embedded.Provider>
    </div>
  );
}
