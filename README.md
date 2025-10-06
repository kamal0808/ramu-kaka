# Ramu Kaka - AI Assistant Ecosystem

Universal ecosystem around automation with AI assistants built with Next.js and CopilotKit.

## Features

- **Agentic Chat UI**: Interactive AI assistant powered by CopilotKit
- **Task Management**: AI can add and manage tasks
- **Real-time State Sharing**: AI has access to application state
- **Modern UI**: Built with Next.js 15, Tailwind CSS, and CopilotKit

## Getting Started

### Prerequisites

- Node.js v22+ (managed with nvm)
- OpenAI API key

### Installation

1. Clone the repository
2. Install dependencies:
   ```bash
   npm install
   ```

3. Set up environment variables:
   ```bash
   cp .env.local.example .env.local
   ```
   Add your OpenAI API key to `.env.local`:
   ```
   OPENAI_API_KEY=your_openai_api_key_here
   ```

4. Start the development server:
   ```bash
   npm run dev
   ```

5. Open [http://localhost:3002](http://localhost:3002) in your browser

## Usage

- The AI assistant (Ramu Kaka) appears in the sidebar
- You can ask it to add tasks, toggle task completion, or answer questions
- The AI has access to the current task list and can modify it
- Try saying: "Add a task to learn React" or "Mark the first task as complete"

## Tech Stack

- **Frontend**: Next.js 15, React, Tailwind CSS
- **AI Framework**: CopilotKit
- **Backend**: Next.js API Routes
- **LLM**: OpenAI GPT (via CopilotKit)
- **Database**: MongoDB (existing dependency)

## Project Structure

```
├── app/
│   ├── api/copilotkit/     # CopilotKit API endpoint
│   ├── globals.css         # Global styles
│   ├── layout.js          # Root layout with CopilotKit provider
│   └── page.js            # Main page with task management
├── .env.local             # Environment variables
├── .nvmrc                 # Node.js version specification
└── tailwind.config.js     # Tailwind CSS configuration
```

## Development

The project uses:
- Node.js v22 (specified in `.nvmrc`)
- Hot reload for development
- CopilotKit for AI integration
- Tailwind CSS for styling

## Next Steps

- Add more AI actions and capabilities
- Integrate with MongoDB for persistent storage
- Add user authentication
- Expand the AI assistant's knowledge base