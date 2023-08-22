const MongoClient = require('mongodb').MongoClient;

// Connection URL
const url = 'mongodb://localhost:27018';

// Database Name
const dbName = 'ramu-kaka';

// Create a new MongoClient
const client = new MongoClient(url);

// Use connect method to connect to the server
client.connect(function(err) {
  if (err) throw err;
  console.log("Connected successfully to server");

  const db = client.db(dbName);
  
  const usersCollection = db.collection('users');
  const sessionsCollection = db.collection('sessions');
  const messagesCollection = db.collection('messages');
});
