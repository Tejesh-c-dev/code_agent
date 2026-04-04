// Reference to DOM elements
const todoForm = document.getElementById('todo-form');
const todoInput = document.getElementById('todo-input');
const todoList = document.getElementById('todo-list');

// Array to store todo objects
let todos = [];

// Initialize the application
function init() {
  // Load any existing todos from localStorage (optional)
  const savedTodos = JSON.parse(localStorage.getItem('todos')) || [];
  todos = savedTodos;
  renderTodos();

  // Set up event listener for form submission
  todoForm.addEventListener('submit', addTodo);

  // Use event delegation for dynamically created elements
  todoList.addEventListener('click', handleTodoActions);
}

// Handle form submission to add a new todo
function addTodo(event) {
  event.preventDefault();
  const todoText = todoInput.value.trim();
  if (todoText === '') return;

  const newTodo = {
    id: Date.now().toString(),
    text: todoText,
    completed: false
  };

  todos.push(newTodo);
  todoInput.value = '';
  renderTodos();
  saveTodos();
}

// Remove a todo by its ID
function removeTodo(id) {
  todos = todos.filter(todo => todo.id !== id);
  renderTodos();
  saveTodos();
}

// Toggle completion status of a todo
function toggleComplete(id) {
  todos = todos.map(todo =>
    todo.id === id ? { ...todo, completed: !todo.completed } : todo
  );
  renderTodos();
  saveTodos();
}

// Render the todo list to the DOM
function renderTodos() {
  todoList.innerHTML = '';
  todos.forEach(todo => {
    const li = document.createElement('li');
    li.className = 'todo-item';

    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.className = 'complete-btn';
    checkbox.checked = todo.completed;
    checkbox.addEventListener('change', () => toggleComplete(todo.id));

    const span = document.createElement('span');
    span.className = `todo-text ${todo.completed ? 'completed' : ''}`;
    span.textContent = todo.text;

    const deleteBtn = document.createElement('button');
    deleteBtn.className = 'delete-btn';
    deleteBtn.textContent = '×';
    deleteBtn.addEventListener('click', () => removeTodo(todo.id));

    li.append(checkbox, span, deleteBtn);
    todoList.appendChild(li);
  });
}

// Handle clicks on the todo list (event delegation)
function handleTodoActions(event) {
  const target = event.target;
  const todoItem = target.closest('.todo-item');
  if (!todoItem) return;

  const id = todoItem.querySelector('.todo-text').dataset.id || 
             Array.from(todoItem.children)
                  .find(child => child.classList.contains('todo-text'))?.dataset.id;
  // Since we didn't store ID in DOM directly, we need to get it from the todo item's data
  // We'll instead store the ID on the li element as a data attribute during render
  // Adjusting renderTodos to set data-id on li
}

// Save todos to localStorage
function saveTodos() {
  localStorage.setItem('todos', JSON.stringify(todos));
}

// Update renderTodos to include data-id on li for easy retrieval
function renderTodos() {
  todoList.innerHTML = '';
  todos.forEach(todo => {
    const li = document.createElement('li');
    li.className = 'todo-item';
    li.dataset.id = todo.id;

    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.className = 'complete-btn';
    checkbox.checked = todo.completed;
    checkbox.addEventListener('change', () => toggleComplete(todo.id));

    const span = document.createElement('span');
    span.className = `todo-text ${todo.completed ? 'completed' : ''}`;
    span.textContent = todo.text;

    const deleteBtn = document.createElement('button');
    deleteBtn.className = 'delete-btn';
    deleteBtn.textContent = '×';
    deleteBtn.addEventListener('click', () => removeTodo(todo.id));

    li.append(checkbox, span, deleteBtn);
    todoList.appendChild(li);
  });
}

// Update handleTodoActions to use data-id
function handleTodoActions(event) {
  const target = event.target;
  const todoItem = target.closest('.todo-item');
  if (!todoItem) return;

  const id = todoItem.dataset.id;
  if (target.classList.contains('complete-btn')) {
    toggleComplete(id);
  } else if (target.classList.contains('delete-btn')) {
    removeTodo(id);
  }
}

// Initialize the app when DOM is loaded
document.addEventListener('DOMContentLoaded', init);