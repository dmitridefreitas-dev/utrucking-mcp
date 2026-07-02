# UTrucking JotForm MCP Server

FastAPI server that lets your Retell AI agent look up student storage orders from JotForm mid-call.

## Deploy to Railway (Free)

1. Go to railway.app and create a free account
2. Click "New Project" → "Deploy from GitHub repo"
3. Upload these files to a GitHub repo first, then connect it
   OR use Railway CLI:
   ```
   npm install -g @railway/cli
   railway login
   railway init
   railway up
   ```

4. In Railway dashboard → your project → Variables, add:
   ```
   JOTFORM_API_KEY=your_new_api_key_here
   ```

5. Railway gives you a public URL like:
   ```
   https://utrucking-mcp-production.up.railway.app
   ```

## Add to Retell

1. Go to your Retell agent → MCP section
2. Click + Add MCP
3. Name: UTrucking_Lookup
4. URL: https://your-railway-url.up.railway.app
5. The Tool Access Scope dropdown will now load
6. Select "lookup_storage_order"
7. Store Fields as Variables:
   - Key: student_name → Value: student_name
   - Key: building → Value: building
   - Key: room → Value: room
   - Key: items → Value: items
   - Key: order_number → Value: order_number
   - Key: message → Value: message

## Agent Prompt to Add

"When a caller asks about their storage order, ask for their full name. 
Call the lookup_storage_order tool using their name. 
Then read back the message field which contains their full order summary."

## Test Locally

```bash
pip install -r requirements.txt
JOTFORM_API_KEY=your_key uvicorn main:app --reload
```

Then visit: http://localhost:8000
