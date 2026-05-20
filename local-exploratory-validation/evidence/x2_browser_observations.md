# X.2 Browser Observations

- Browser: VS Code integrated browser
- Page title: `OMNI Agent — Control Panel`
- URL exercised: `http://127.0.0.1:8765/dashboard`
- Overview loaded with live runtime values (`running`, `healthy`, model count `27`, jobs `2`)
- Save API key control displayed `✓ saved for this tab`
- Models tab loaded the protected catalog after synthetic local auth was active
- Chat send via button rendered a normal assistant reply
- Chat send via Enter rendered a normal assistant reply
- Clear chat reset the transcript without a page error
- Load History displayed readable JSON output (`{ "memories": [] }`)
- Invalid pipeline execution displayed a readable JSON error in the page output area
- No literal `[object Object]` string was found in full-page text after exercised interactions
- No CSP violation surfaced during normal interactions observed in this pass
- One browser console error surfaced during the intentional missing-pipeline negative test: `Failed to load resource: the server responded with a status of 404 (Not Found)`
- No screenshots captured in this pass
