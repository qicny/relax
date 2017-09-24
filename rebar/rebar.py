import autograd.numpy as np
import autograd.numpy.random as npr

from autograd.scipy.special import expit, logit
from autograd import grad, primitive, value_and_grad, make_vjp


def heaviside(z):
    return z >= 0

def relaxed_heaviside(z, log_temperature):  # sigma_lambda in REBAR paper.
    temperature = np.exp(log_temperature)   # TODO: get rid of naked exp
    return expit(z / temperature)

def logistic_sample(logit_theta, noise):  # REBAR's z = g(theta, u)
    return logit_theta + logit(noise)

def bernoulli_sample(logit_theta, noise):
    return logit(noise) < logit_theta  # heaviside(logistic_sample(logit_theta, noise))

def relaxed_bernoulli_sample(logit_theta, noise, log_temperature):
    return relaxed_heaviside(logistic_sample(logit_theta, noise), log_temperature)

def conditional_noise(logit_theta, samples, noise):
    # Computes p(u|b), where b = H(z), z = logit_theta + logit(noise), p(u) = U(0, 1)
    uprime = expit(-logit_theta)  # u' = 1 - theta
    return samples * (noise * (1 - uprime) + uprime) + (1 - samples) * noise * uprime

def bernoulli_logprob(logit_theta, targets):
    # log Ber(targets | theta), targets are 0 or 1.
    return -np.logaddexp(0, -logit_theta * (targets * 2 - 1))


############### REINFORCE ##################

def reinforce(params, noise, f):
    samples = bernoulli_sample(params, noise)
    func_vals, grad_func_vals = value_and_grad(f)(params, samples)
    return grad_func_vals + func_vals * grad(bernoulli_logprob)(params, samples)


############### CONCRETE ###################

def concrete(params, log_temperature, noise, f):
    relaxed_samples = relaxed_bernoulli_sample(params, noise, log_temperature)
    return f(params, relaxed_samples)


############### REBAR ######################

def rebar(model_params, est_params, noise_u, noise_v, f):
    log_temperature, log_eta = est_params
    eta = np.exp(log_eta)
    samples = bernoulli_sample(model_params, noise_u)

    def concrete_cond(params):
        cond_noise = conditional_noise(params, samples, noise_v)  # z tilde
        return concrete(params, log_temperature, cond_noise, f)

    grad_concrete = grad(concrete)(model_params, log_temperature, noise_u, f)  # d_f(z) / d_theta
    f_cond, grad_concrete_cond = value_and_grad(concrete_cond)(model_params)  # d_f(ztilde) / d_theta
    controlled_f = lambda params, samples: f(params, samples) - eta * f_cond

    return reinforce(model_params, noise_u, controlled_f) \
           + eta * grad_concrete - eta * grad_concrete_cond

def grad_of_var_of_grads(grads):
    # For an unbiased gradient estimator, gives and unbiased
    # single-sample estimate of the gradient of the variance of the gradients.
    return 2 * grads / grads.shape[0]



############## Hooks into autograd ##############

###Set up Simple Monte Carlo functions that have different gradient estimators
@primitive
def simple_mc_reinforce(params, noise, f):
    samples = bernoulli_sample(params, noise)
    return f(params, samples)

def reinforce_vjp(g, ans, vs, gvs, *args):
    return g * reinforce(*args)
simple_mc_reinforce.defvjp(reinforce_vjp)


@primitive
def simple_mc_rebar(model_params, est_params, noise_u, noise_v, f):
    samples = bernoulli_sample(model_params, noise_u)
    return f(model_params, samples)

def rebar_vjp(g, ans, vs, gvs, *args):
    return g * rebar(*args)
simple_mc_rebar.defvjp(rebar_vjp, argnum=0)
#simple_mc_rebar.defvjp_is_zero(argnums=(1,))

def rebar_v_vjp(g, ans, vs, gvs, *args):
    # Unbiased estimator of grad of variance of rebar.
    grads = rebar(*args)
    grad_est = grad_of_var_of_grads(grads)
    est_params_vjp, _ = make_vjp(rebar, argnum=1)(*args)
    return est_params_vjp(grad_est)

simple_mc_rebar.defvjp(rebar_v_vjp, argnum=1)

def obj_rebar_estgrad_var(model_params, est_params, noise_u, noise_v, f):
    # To avoid recomputing things, here's a function that computes everything together
    samples = bernoulli_sample(model_params, noise_u)
    #obj = f(model_params, samples)
    #grads, estgrad = value_and_grad(rebar, argnum=1)(model_params, est_params, noise_u, noise_v, f)
    rebar_list = []
    def value_and_rebar(est_params):
        o, r = value_and_grad(simple_mc_rebar)(model_params, est_params, noise_u, noise_v, f)
        rebar_list.append(r)
        return r


    obj, grads = rebar_list[0]
    vargrad = make_vjp(value_and_rebar)(est_params)(grad_of_var_of_grads(grads))

    #vargrad = estgrad *
    var = np.var(grads, axis=0)
    return obj, grads, vargrad, var



def rebar_var_vjp(g, ans, vs, gvs, *args):
    # Unbiased estimator of grad of variance of rebar.
    _, grads, _ = ans
    _, _, var_g = g
    grad_est = grad_of_var_of_grads(grads)
    est_params_vjp, _ = make_vjp(rebar, argnum=1)(*args)
    return est_params_vjp(grad_est)

    def double_val_fun(*args):
        val = fun(*args)
        return make_tuple(val, unbox_if_possible(val))
    gradval_and_val = grad_and_aux(double_val_fun, argnum)
    flip = lambda x, y: make_tuple(y, x)
    return lambda *args: flip(*gradval_and_val(*args))

# This wrapper lets us implement the single-sample estimator
# of the gradient of the variance of the gradient estimate from the paper.
@primitive
def simple_mc_rebar_grads_var(*args):
    # Returns estimates of objective, gradients, and variance of gradients.
    obj, grads = value_and_grad(simple_mc_rebar)(*args)
    return obj, grads, np.var(grads, axis=0)

def rebar_obj_vjp((obj_g, rebar_g, var_g), (obj, grads, var), vs, gvs, *args):
    return obj_g * grads

def rebar_var_vjp(g, ans, vs, gvs, *args):
    # Unbiased estimator of grad of variance of rebar.
    _, grads, _ = ans
    _, _, var_g = g
    grad_est = 2 * var_g * grads / grads.shape[0]  # Formula from paper
    est_params_vjp, _ = make_vjp(rebar, argnum=1)(*args)
    return est_params_vjp(grad_est)
simple_mc_rebar_grads_var.defvjp(rebar_obj_vjp, argnum=0)
simple_mc_rebar_grads_var.defvjp(rebar_var_vjp, argnum=1)



############### SIMPLE REBAR ######################
# Doesn't use concrete distribution at all, still unbiased

def conditional_noise_uniform(logit_theta, samples, noise):
    # Computes p(u|b) where b = H(u < theta), p(u) = U(0, 1)
    # p(z | b = 0) = U(theta, 1)
    # p(z | b = 1) = U(0, theta)
    theta = expit(logit_theta)
    return (1 - samples) * (noise * (1 - theta) + theta) + samples * noise * theta

def simple_rebar(model_params, noise_u, noise_v, f):
    samples = bernoulli_sample(model_params, noise_u)

    def noise_cond(params):
        cond_noise = conditional_noise_uniform(params, samples, noise_v)
        return f(params, cond_noise)

    grad_noise = grad(f)(model_params, noise_u)
    f_cond, grad_noise_cond = value_and_grad(noise_cond)(model_params)
    controlled_f = lambda params, samples: f(params, samples) - f_cond
    return reinforce(model_params, noise_u, controlled_f) + grad_noise - grad_noise_cond

@primitive
def simple_mc_simple_rebar(model_params, noise_u, noise_v, f):
    samples = bernoulli_sample(model_params, noise_u)
    return f(model_params, samples)

def simple_rebar_vjp(g, ans, vs, gvs, model_params, noise_u, noise_v, f):
    return g * simple_rebar(model_params, noise_u, noise_v, f)
simple_mc_simple_rebar.defvjp(simple_rebar_vjp, argnum=0)
simple_mc_simple_rebar.defvjp_is_zero(argnums=(1,))



############### GENERALIZED REBAR ######################
# Uses a neural network for control variate instead of original objective
# Question: Should f tilde depend on model_params?

def init_nn_params(scale, layer_sizes, rs=npr.RandomState(0)):
    """Build a list of (weights, biases) tuples, one for each layer."""
    return [(rs.randn(insize, outsize) * scale,   # weight matrix
             rs.randn(outsize) * scale)           # bias vector
            for insize, outsize in zip(layer_sizes[:-1], layer_sizes[1:])]

def nn_predict(params, inputs):
    for W, b in params:
        outputs = np.dot(inputs, W) + b
        inputs = np.tanh(outputs)
    return outputs

def func_plus_nn(model_params, relaxed_samples, nn_scale, nn_params, f):
    return f(model_params, relaxed_samples) \
           + nn_scale * nn_predict(nn_params, relaxed_samples)

def generalized_rebar(model_params, est_params, noise_u, noise_v, f):
    samples = bernoulli_sample(model_params, noise_u)
    log_eta, log_temperature, log_nn_scale, nn_params = est_params
    eta = np.exp(log_eta)
    nn_scale = np.exp(log_nn_scale)

    def f_relaxed(model_params, relaxed_samples):
        return func_plus_nn(model_params, relaxed_samples, nn_scale, nn_params, f)

    def concrete_cond(params):
        cond_noise = conditional_noise(params, samples, noise_v)  # z tilde
        return concrete(params, log_temperature, cond_noise, f_relaxed)

    grad_concrete = grad(concrete)(model_params, log_temperature, noise_u, f_relaxed)
    f_cond, grad_concrete_cond = value_and_grad(concrete_cond)(model_params)
    controlled_f = lambda params, samples: f(params, samples) - eta * f_cond

    return reinforce(model_params, noise_u, controlled_f) \
           + eta * grad_concrete - eta * grad_concrete_cond

@primitive
def simple_mc_generalized_rebar(model_params, est_params, noise_u, noise_v, f):
    samples = bernoulli_sample(model_params, noise_u)
    return f(model_params, samples)

def generalized_rebar_vjp(g, ans, vs, gvs, *args):
    return g * generalized_rebar(*args)
simple_mc_generalized_rebar.defvjp(generalized_rebar_vjp, argnum=0)
simple_mc_generalized_rebar.defvjp_is_zero(argnums=(1,))


@primitive
def gen_rebar_grads_var(*args):
    # Returns estimates of objective, gradients, and variance of gradients.
    obj, grads = value_and_grad(simple_mc_generalized_rebar)(*args)
    return obj, grads, np.var(grads, axis=0)

def gen_rebar_var_vjp(g, ans, vs, gvs, *args):
    # Unbiased estimator of grad of variance of rebar.
    _, grads, _ = ans
    _, _, var_g = g
    grad_est = 2 * var_g * grads / grads.shape[0]  # Formula from paper
    est_params_vjp, _ = make_vjp(generalized_rebar, argnum=1)(*args)
    return est_params_vjp(grad_est)
gen_rebar_grads_var.defvjp(rebar_obj_vjp, argnum=0)
gen_rebar_grads_var.defvjp(gen_rebar_var_vjp, argnum=1)
