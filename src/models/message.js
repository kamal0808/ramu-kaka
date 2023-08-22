const mongoose = require('mongoose');

const { Schema } = mongoose;
const { ObjectId } = mongoose.Types;

const messageSchema = new Schema({
    "sessionId": ObjectId(),
    "message": String,
    "fromUser": Boolean,
    createdAt: { type: Date, default: () => new Date() },
    updatedAt: { type: Date, default: () => new Date() },
    deletedAt: { type: Date, default: null },
});

const Messages = mongoose.model('Messages', messageSchema);

module.exports = Messages;
