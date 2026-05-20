# Authenticated malformed /chat probe command

curl.exe -i -X POST http://127.0.0.1:8765/chat \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <LOCAL_TEST_TOKEN>" \
  --data-binary @"local-exploratory-validation/evidence/bugfix-chat-json/c4_malformed_chat_payload.txt"
