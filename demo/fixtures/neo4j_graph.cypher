// params: {"weaviate_id": "05e550ee-acda-53c5-853e-f974d68bae88", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:01+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;

// params: {"weaviate_id": "b2ef5440-592d-5983-8dab-efc05a887cc6", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:01+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;

// params: {"weaviate_id": "9744283e-fb9a-544e-92f4-3da44c7451c5", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:02+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;

// params: {"weaviate_id": "ef4a7cff-c920-5065-abdf-142c9420c62d", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:02+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;

// params: {"weaviate_id": "57ed3b0f-f629-508f-a0a4-fa7f08cb16c3", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:03+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;

// params: {"weaviate_id": "5aee637c-2a0a-540e-a074-6678bf7761c7", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:03+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;

// params: {"weaviate_id": "ab9eac51-7226-5459-b111-fa685e46b5cf", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:04+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;

// params: {"weaviate_id": "589b6241-dee2-5949-a4cd-902d86440060", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:04+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;

// params: {"weaviate_id": "d44dfce2-12f4-5e9c-a55d-9509ac30ca97", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:05+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;

// params: {"weaviate_id": "b7be7d4a-bde4-5df8-b84a-147feb4009bd", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:05+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;

// params: {"weaviate_id": "068cb7e1-e800-5400-8a98-87cc86c8a3c2", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:06+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;

// params: {"weaviate_id": "9cb7ea2b-bf87-5dff-8696-2e0cd2d97246", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:06+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;

// params: {"weaviate_id": "bee3b747-7f2e-55bc-ba0c-bd386c8ac2f9", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:07+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;

// params: {"weaviate_id": "20c45581-0eed-5cca-af58-5e6db4257aa1", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:07+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;

// params: {"media_type": "link", "title": "en.wikipedia.org/wiki/Ada_Lovelace", "channel_id": "demo-wikipedia", "url": "https://en.wikipedia.org/wiki/Ada_Lovelace", "message_ts": "2026-01-01T00:00:01+00:00"}
MERGE (n:Media {name: $name}) SET n.media_type = $media_type, n.title = $title, n.channel_id = $channel_id, n.url = $url, n.message_ts = $message_ts;

// params: {"media_type": "link", "title": "creativecommons.org/licenses/by-sa", "channel_id": "demo-wikipedia", "url": "https://creativecommons.org/licenses/by-sa/3.0/", "message_ts": "2026-01-01T00:00:01+00:00"}
MERGE (n:Media {name: $name}) SET n.media_type = $media_type, n.title = $title, n.channel_id = $channel_id, n.url = $url, n.message_ts = $message_ts;

// params: {"media_type": "link", "title": "en.wikipedia.org/wiki/Analytical_Engine", "channel_id": "demo-wikipedia", "url": "https://en.wikipedia.org/wiki/Analytical_Engine", "message_ts": "2026-01-01T00:00:03+00:00"}
MERGE (n:Media {name: $name}) SET n.media_type = $media_type, n.title = $title, n.channel_id = $channel_id, n.url = $url, n.message_ts = $message_ts;

// params: {"media_type": "link", "title": "en.wikipedia.org/wiki/Charles_Babbage", "channel_id": "demo-wikipedia", "url": "https://en.wikipedia.org/wiki/Charles_Babbage", "message_ts": "2026-01-01T00:00:04+00:00"}
MERGE (n:Media {name: $name}) SET n.media_type = $media_type, n.title = $title, n.channel_id = $channel_id, n.url = $url, n.message_ts = $message_ts;

// params: {"media_type": "link", "title": "en.wikipedia.org/wiki/Guido_van_Rossum", "channel_id": "demo-wikipedia", "url": "https://en.wikipedia.org/wiki/Guido_van_Rossum", "message_ts": "2026-01-01T00:00:05+00:00"}
MERGE (n:Media {name: $name}) SET n.media_type = $media_type, n.title = $title, n.channel_id = $channel_id, n.url = $url, n.message_ts = $message_ts;

// params: {"media_type": "link", "title": "en.wikipedia.org/wiki/Python_(programming_language", "channel_id": "demo-wikipedia", "url": "https://en.wikipedia.org/wiki/Python_(programming_language", "message_ts": "2026-01-01T00:00:06+00:00"}
MERGE (n:Media {name: $name}) SET n.media_type = $media_type, n.title = $title, n.channel_id = $channel_id, n.url = $url, n.message_ts = $message_ts;

// params: {"media_type": "link", "title": "en.wikipedia.org/wiki/History_of_Python", "channel_id": "demo-wikipedia", "url": "https://en.wikipedia.org/wiki/History_of_Python", "message_ts": "2026-01-01T00:00:07+00:00"}
MERGE (n:Media {name: $name}) SET n.media_type = $media_type, n.title = $title, n.channel_id = $channel_id, n.url = $url, n.message_ts = $message_ts;

// params: {"weaviate_id": "639aa329-1985-5fbd-ade5-a21e92afd482", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:01+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;

// params: {"weaviate_id": "47317a32-f76a-5e01-8bdc-64af64e13201", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:02+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;

// params: {"weaviate_id": "726c5744-23b4-5382-9cbf-519fd8b908a5", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:03+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;

// params: {"weaviate_id": "2e0f3d84-57e7-547d-9c43-cbbce679ec1c", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:03+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;

// params: {"weaviate_id": "7adeb7db-febc-502a-821a-a99bd45a9a42", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:04+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;

// params: {"weaviate_id": "fbe15806-839b-532c-8e00-da9b27b5b04d", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:04+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;

// params: {"weaviate_id": "313bc4b1-ffab-5243-8538-f1b9cda74e3a", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:05+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;

// params: {"weaviate_id": "aef0adc6-16d6-5613-aad7-ff2e009554cc", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:05+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;

// params: {"weaviate_id": "7dd769f8-a1b4-5c23-90fc-bf868beee2e2", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:06+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;

// params: {"weaviate_id": "480e30b3-feda-5e0b-851e-4804f7a58c92", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:06+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;

// params: {"weaviate_id": "bf454ed1-dc78-5f3c-90e1-df94bc4252e8", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:06+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;

// params: {"weaviate_id": "7b64a3b3-6454-535a-a0d9-3b8b92d13048", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:07+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;

// params: {"weaviate_id": "54b1899a-a504-5e42-b31d-fe0756224681", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:07+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;

// params: {"weaviate_id": "77f0ce1c-3b10-5321-ac6b-75ff256d719b", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:07+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;
