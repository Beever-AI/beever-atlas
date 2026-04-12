import { useParams } from "react-router-dom";
import { AskCore } from "./AskCore";

export function AskTab() {
  const { id: channelId = "" } = useParams<{ id: string }>();
  return <AskCore channelMode="fixed" channelId={channelId} />;
}

export default AskTab;
