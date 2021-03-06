# Top-down RvNN implementation based on the model of Jing Ma et al.
# (https://github.com/majingCUHK/Rumor_RvNN ; state: 10.09.2019)

import numpy as np
import theano
from theano import tensor as T
from collections import OrderedDict
from theano.tensor.signal.pool import pool_2d
from time import time


theano.config.floatX = 'float32'


class Node_tweet(object):
    def __init__(self, idx=None):
        self.children = []
        self.idx = idx
        self.word = []
        self.index = []
        self.parent = None
        
################################# generate tree structure ##############################
#def gen_nn_inputs(root_node, ini_word, ini_index):
def gen_nn_inputs(root_node, ini_word):
    """Given a root node, returns the appropriate inputs to NN.

    The NN takes in
        x: the values at the leaves (e.g. word indices)
        tree: a (n x degree) matrix that provides the computation order.
            Namely, a row tree[i] = [a, b, c] in tree signifies that a
            and b are children of c, and that the computation
            f(a, b) -> c should happen on step i.

    """
    tree = [[0, root_node.idx]] 
    X_word, X_index = [root_node.word], [root_node.index]
    internal_tree, internal_word, internal_index  = _get_tree_path(root_node)
    tree.extend(internal_tree)    
    X_word.extend(internal_word)
    X_index.extend(internal_index)
    X_word.append(ini_word)
    return (np.array(X_word, dtype='float32'),
            np.array(X_index, dtype='int32'),
            np.array(tree, dtype='int32'))

def _get_tree_path(root_node):
    """Get computation order of leaves -> root."""
    if not root_node.children:
        return [], [], []
    layers = []
    layer = [root_node]
    while layer:
        layers.append(layer[:])
        next_layer = []
        [next_layer.extend([child for child in node.children if child])
         for node in layer]
        layer = next_layer
    #print 'layer:', layers
    tree = []
    word = []
    index = []
    for layer in layers:
        for node in layer:
            if not node.children:
               continue 
            #child_idxs = [child.idx for child in ]  ## idx of child node
            for child in node.children:
                tree.append([node.idx, child.idx])
                word.append(child.word if child.word is not None else -1)
                index.append(child.index if child.index is not None else -1)

    return tree, word, index

################################ tree rnn class ######################################
class RvNN(object):
    """Data is represented in a tree structure.

    Every leaf and internal node has a data (provided by the input)
    and a memory or hidden state.  The hidden state is computed based
    on its own data and the hidden states of its children.  The
    hidden state of leaves is given by a custom init function.

    The entire tree's embedding is represented by the final
    state computed at the root.

    """
    def __init__(self, word_dim, hidden_dim=5, Nclass=3,
                degree=2, momentum=0.9,
                 trainable_embeddings=True,
                 labels_on_nonroot_nodes=False,
                 irregular_tree=True):                 
        assert word_dim > 1 and hidden_dim > 1
        self.word_dim = word_dim
        self.hidden_dim = hidden_dim
        self.Nclass = Nclass
        self.degree = degree
        self.momentum = momentum
        self.irregular_tree = irregular_tree

        self.params = []
        self.x_word = T.matrix(name='x_word')  # word frequent
        self.x_index = T.imatrix(name='x_index')  # word indices
        self.tree = T.imatrix(name='tree')  # shape [None, self.degree]
        self.y = T.ivector(name='y')  # output shape [self.output_dim]
        self.num_parent = T.iscalar(name='num_parent')
        self.num_nodes = self.x_word.shape[0]  # total number of nodes (leaves + internal) in tree
        self.num_child = self.num_nodes - self.num_parent-1

        self.tree_states = self.compute_tree(self.x_word, self.x_index, self.num_parent, self.tree)
        self.final_state = self.tree_states.max(axis=0)
        self.output_fn = self.create_output_fn()
        self.pred_y = self.output_fn(self.final_state)
        self.loss = self.loss_fn(self.y, self.pred_y)

        self.learning_rate = T.scalar('learning_rate')
        train_inputs = [self.x_word, self.x_index, self.num_parent, self.tree, self.y, self.learning_rate]
        updates = self.gradient_descent(self.loss)

        self._train = theano.function(train_inputs,
                                      [self.loss, self.pred_y],
                                      updates=updates)

        self._evaluate = theano.function([self.x_word, self.x_index, self.num_parent, self.tree], self.final_state)
        self._evaluate2 = theano.function([self.x_word, self.x_index, self.num_parent, self.tree], self.tree_states)

        self._predict = theano.function([self.x_word, self.x_index, self.num_parent, self.tree], self.pred_y)
        
        self.tree_states_test = self.compute_tree_test(self.x_word, self.x_index, self.tree)
        self._evaluate3 = theano.function([self.x_word, self.x_index, self.tree], self.tree_states_test)
    
    def train_step_up(self, x_word, x_index, num_parent, tree, y, lr):
        return self._train(x_word, x_index, num_parent, tree, y, lr)
        
    def evaluate(self,  x_word, x_index, num_parent, tree):
        #self._check_input(x, tree)
        return self._evaluate(x_word, x_index, num_parent, tree)

    def predict_up(self, x_word, x_index, num_parent, tree):
        return self._predict(x_word, x_index, num_parent, tree)

    def init_matrix(self, shape):
        return np.random.normal(scale=0.1, size=shape).astype(theano.config.floatX)

    def init_vector(self, shape):
        return np.zeros(shape, dtype=theano.config.floatX)

    def create_output_fn(self):
        self.W_out = theano.shared(self.init_matrix([self.Nclass, self.hidden_dim]))
        self.b_out = theano.shared(self.init_vector([self.Nclass]))
        self.params.extend([self.W_out, self.b_out])

        def fn(final_state):
            return T.nnet.softmax( self.W_out.dot(final_state)+ self.b_out )
        return fn

    def create_recursive_unit(self):
        self.E = theano.shared(self.init_matrix([self.hidden_dim, self.word_dim]))
        self.W_z = theano.shared(self.init_matrix([self.hidden_dim, self.hidden_dim]))
        self.U_z = theano.shared(self.init_matrix([self.hidden_dim, self.hidden_dim]))
        self.b_z = theano.shared(self.init_vector([self.hidden_dim]))
        self.W_r = theano.shared(self.init_matrix([self.hidden_dim, self.hidden_dim]))
        self.U_r = theano.shared(self.init_matrix([self.hidden_dim, self.hidden_dim]))
        self.b_r = theano.shared(self.init_vector([self.hidden_dim]))
        self.W_h = theano.shared(self.init_matrix([self.hidden_dim, self.hidden_dim]))
        self.U_h = theano.shared(self.init_matrix([self.hidden_dim, self.hidden_dim]))
        self.b_h = theano.shared(self.init_vector([self.hidden_dim]))
        self.params.extend([self.E, self.W_z, self.U_z, self.b_z, self.W_r, self.U_r, self.b_r, self.W_h, self.U_h, self.b_h])
        def unit(word, index, parent_h):
            child_xe = self.E[:,index].dot(word)
            z = T.nnet.hard_sigmoid(self.W_z.dot(child_xe)+self.U_z.dot(parent_h)+self.b_z)
            r = T.nnet.hard_sigmoid(self.W_r.dot(child_xe)+self.U_r.dot(parent_h)+self.b_r)
            c = T.tanh(self.W_h.dot(child_xe)+self.U_h.dot(parent_h * r)+self.b_h)
            h = z*parent_h + (1-z)*c
            return h
        return unit

    def compute_tree(self, x_word, x_index, num_parent, tree):
        self.recursive_unit = self.create_recursive_unit()
        def ini_unit(x):
            return theano.shared(self.init_vector([self.hidden_dim])) 
        init_node_h, _ = theano.scan(
            fn=ini_unit,
            sequences=[ x_word ])

        # use recurrence to compute internal node hidden states
        def _recurrence(x_word, x_index, node_info, node_h, last_h):
            parent_h = node_h[node_info[0]]
            child_h = self.recursive_unit(x_word, x_index, parent_h)
            node_h = T.concatenate([node_h[:node_info[1]],
                                    child_h.reshape([1, self.hidden_dim]),
                                    node_h[node_info[1]+1:] ])
            return node_h, child_h

        dummy = theano.shared(self.init_vector([self.hidden_dim]))
        (_, child_hs), _ = theano.scan(
            fn=_recurrence,
            outputs_info=[init_node_h, dummy],
            sequences=[x_word[:-1], x_index, tree])
        return child_hs[num_parent-1:]

    def compute_tree_test(self, x_word, x_index, tree):
        self.recursive_unit = self.create_recursive_unit()
        def ini_unit(x):
            return theano.shared(self.init_vector([self.hidden_dim]))
        init_node_h, _ = theano.scan(
            fn=ini_unit,
            sequences=[ x_word ])

        def _recurrence(x_word, x_index, node_info, node_h, last_h):
            parent_h = node_h[node_info[0]]
            child_h = self.recursive_unit(x_word, x_index, parent_h)
            node_h = T.concatenate([node_h[:node_info[1]],
                                    child_h.reshape([1, self.hidden_dim]),
                                    node_h[node_info[1]+1:] ])
            return node_h, child_h

        dummy = theano.shared(self.init_vector([self.hidden_dim]))
        (_, child_hs), _ = theano.scan(
            fn=_recurrence,
            outputs_info=[init_node_h, dummy],
            sequences=[x_word[:-1], x_index, tree])
        return child_hs
        
    def loss_fn(self, y, pred_y):
        return T.sum(T.sqr(y - pred_y))

    def gradient_descent(self, loss):
        """Momentum GD with gradient clipping."""
        grad = T.grad(loss, self.params)
        self.momentum_velocity_ = [0.] * len(grad)
        grad_norm = T.sqrt(sum(map(lambda x: T.sqr(x).sum(), grad)))
        updates = OrderedDict()
        not_finite = T.or_(T.isnan(grad_norm), T.isinf(grad_norm))
        scaling_den = T.maximum(5.0, grad_norm)
        for n, (param, grad) in enumerate(zip(self.params, grad)):
            grad = T.switch(not_finite, 0.1 * param,
                            grad * (5.0 / scaling_den))
            velocity = self.momentum_velocity_[n]
            update_step = self.momentum * velocity - self.learning_rate * grad
            self.momentum_velocity_[n] = update_step
            updates[param] = param + update_step
        return updates
        
def establish_model(vocabulary_size: int, hidden_dim: int, Nclass: int):
    print("Establishing recursive model...")
    time_before = time()
    model = RvNN(vocabulary_size, hidden_dim, Nclass)
    time_after = time()
    print("  Took {:.2f}s.".format(time_after - time_before))
    return model
