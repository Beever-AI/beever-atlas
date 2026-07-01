// params: {"media_type": "link", "title": "en.wikipedia.org/wiki/Ada_Lovelace", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:01+00:00", "url": "https://en.wikipedia.org/wiki/Ada_Lovelace"}
MERGE (n:Media {name: $name}) SET n.media_type = $media_type, n.title = $title, n.channel_id = $channel_id, n.message_ts = $message_ts, n.url = $url;

// params: {"media_type": "link", "title": "creativecommons.org/licenses/by-sa", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:01+00:00", "url": "https://creativecommons.org/licenses/by-sa/3.0/"}
MERGE (n:Media {name: $name}) SET n.media_type = $media_type, n.title = $title, n.channel_id = $channel_id, n.message_ts = $message_ts, n.url = $url;

// params: {"media_type": "link", "title": "en.wikipedia.org/wiki/Analytical_Engine", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:03+00:00", "url": "https://en.wikipedia.org/wiki/Analytical_Engine"}
MERGE (n:Media {name: $name}) SET n.media_type = $media_type, n.title = $title, n.channel_id = $channel_id, n.message_ts = $message_ts, n.url = $url;

// params: {"media_type": "link", "title": "en.wikipedia.org/wiki/Charles_Babbage", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:04+00:00", "url": "https://en.wikipedia.org/wiki/Charles_Babbage"}
MERGE (n:Media {name: $name}) SET n.media_type = $media_type, n.title = $title, n.channel_id = $channel_id, n.message_ts = $message_ts, n.url = $url;

// params: {"media_type": "link", "title": "en.wikipedia.org/wiki/Guido_van_Rossum", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:05+00:00", "url": "https://en.wikipedia.org/wiki/Guido_van_Rossum"}
MERGE (n:Media {name: $name}) SET n.media_type = $media_type, n.title = $title, n.channel_id = $channel_id, n.message_ts = $message_ts, n.url = $url;

// params: {"media_type": "link", "title": "en.wikipedia.org/wiki/Python_(programming_language", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:06+00:00", "url": "https://en.wikipedia.org/wiki/Python_(programming_language"}
MERGE (n:Media {name: $name}) SET n.media_type = $media_type, n.title = $title, n.channel_id = $channel_id, n.message_ts = $message_ts, n.url = $url;

// params: {"media_type": "link", "title": "en.wikipedia.org/wiki/History_of_Python", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:07+00:00", "url": "https://en.wikipedia.org/wiki/History_of_Python"}
MERGE (n:Media {name: $name}) SET n.media_type = $media_type, n.title = $title, n.channel_id = $channel_id, n.message_ts = $message_ts, n.url = $url;

// params: {"weaviate_id": "86850fd7-afa4-573e-9b5a-f345f2c5b20c", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:01+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;

// params: {"weaviate_id": "48826787-c1ee-58cc-98d3-ea51b416c19a", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:01+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;

// params: {"weaviate_id": "a8b8e713-0e44-5f65-9016-d5dbb1a33f06", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:01+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;

// params: {"weaviate_id": "36acce8f-0b16-5ad2-b7ee-38f2e589c60e", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:02+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;

// params: {"weaviate_id": "309688d8-971c-5790-9fab-6dd2db06e59c", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:02+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;

// params: {"weaviate_id": "02b4ab48-59f1-53f9-9b0a-16902d0bde5c", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:03+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;

// params: {"weaviate_id": "5130d5fe-0d0c-5473-b935-7cddc63b9a16", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:03+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;

// params: {"weaviate_id": "3d5bdba9-e0d6-52f2-8853-d2425f8682fc", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:03+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;

// params: {"weaviate_id": "0cb8b287-1c98-56f6-8c6e-8a95df6cc51e", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:04+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;

// params: {"weaviate_id": "23fb8b6d-8d3b-5f51-a826-50fe951c604a", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:04+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;

// params: {"weaviate_id": "5ccce50f-4d92-568f-89ab-2fa194cc96db", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:04+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;

// params: {"weaviate_id": "8804f688-e7bb-5cca-a692-85fc9df624c7", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:05+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;

// params: {"weaviate_id": "7b651a0a-a9f9-5354-8fec-35606f2a14d0", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:05+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;

// params: {"weaviate_id": "c1945ee8-1a89-5996-a7be-e1d3bb80a3e0", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:05+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;

// params: {"weaviate_id": "8a79cf73-a1a4-53a9-91dd-d6e8e1528113", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:06+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;

// params: {"weaviate_id": "560558c5-3093-5c9e-acad-f38dffa044e7", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:06+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;

// params: {"weaviate_id": "4b2e5703-b9d7-5b45-b9a2-0c2b22f9ba5c", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:06+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;

// params: {"weaviate_id": "9cb3b227-a31c-582b-8351-deeedbb35fef", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:07+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;

// params: {"weaviate_id": "7d8f8045-e291-59a1-95ab-35720d3ab264", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:07+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;

// params: {"weaviate_id": "94eeab97-019b-516c-a32c-81cf1aa8dd84", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:07+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;

// params: {"weaviate_id": "d5b701d8-668d-5760-8684-1dc6b6a0eeb7", "channel_id": "demo-wikipedia", "message_ts": "2026-01-01T00:00:07+00:00"}
MERGE (n:Event {name: $name}) SET n.weaviate_id = $weaviate_id, n.channel_id = $channel_id, n.message_ts = $message_ts;
