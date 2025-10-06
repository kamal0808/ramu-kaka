'use client'

import { CopilotSidebar } from '@copilotkit/react-ui'
import { useCopilotReadable, useCopilotAction } from '@copilotkit/react-core'
import { useState } from 'react'

export default function Home() {
  const [tasks, setTasks] = useState([
    { id: 1, text: 'Learn about CopilotKit', completed: false },
    { id: 2, text: 'Build an AI assistant', completed: false },
  ])

  // Make tasks readable by the AI
  useCopilotReadable({
    description: 'The current list of tasks',
    value: tasks,
  })

  // Allow AI to add tasks
  useCopilotAction({
    name: 'addTask',
    description: 'Add a new task to the list',
    parameters: [
      {
        name: 'text',
        type: 'string',
        description: 'The text of the task to add',
        required: true,
      },
    ],
    handler: ({ text }) => {
      const newTask = {
        id: Date.now(),
        text,
        completed: false,
      }
      setTasks(prev => [...prev, newTask])
    },
  })

  // Allow AI to toggle task completion
  useCopilotAction({
    name: 'toggleTask',
    description: 'Toggle the completion status of a task',
    parameters: [
      {
        name: 'id',
        type: 'number',
        description: 'The ID of the task to toggle',
        required: true,
      },
    ],
    handler: ({ id }) => {
      setTasks(prev =>
        prev.map(task =>
          task.id === id ? { ...task, completed: !task.completed } : task
        )
      )
    },
  })

  return (
    <div className="flex h-screen">
      <main className="flex-1 p-8">
        <h1 className="text-3xl font-bold mb-4">Welcome to Ramu Kaka</h1>
        <p className="text-gray-600 mb-8">Universal ecosystem around automation with AI assistants</p>

        <div className="max-w-md">
          <h2 className="text-xl font-semibold mb-4">Tasks</h2>
          <ul className="space-y-2">
            {tasks.map(task => (
              <li key={task.id} className="flex items-center space-x-2">
                <input
                  type="checkbox"
                  checked={task.completed}
                  onChange={() => {
                    setTasks(prev =>
                      prev.map(t =>
                        t.id === task.id ? { ...t, completed: !t.completed } : t
                      )
                    )
                  }}
                  className="rounded"
                />
                <span className={task.completed ? 'line-through text-gray-500' : ''}>
                  {task.text}
                </span>
              </li>
            ))}
          </ul>
        </div>
      </main>

      <CopilotSidebar
        defaultOpen={true}
        instructions="You are Ramu Kaka, a helpful AI assistant. You can help manage tasks, answer questions, and assist with automation. You can add new tasks or toggle existing ones."
        labels={{
          title: "Ramu Kaka Assistant",
          initial: "Hello! I'm Ramu Kaka, your AI assistant. I can help you manage tasks and answer questions. Try asking me to add a task or toggle one!",
        }}
      />
    </div>
  )
}
