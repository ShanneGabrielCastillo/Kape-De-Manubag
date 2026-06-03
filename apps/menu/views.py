from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse
from django.db.models import Q
from .models import Category, Product
from .forms import ProductForm, CategoryForm
from apps.accounts.decorators import admin_required


def menu_index(request):
    """Public-facing menu page"""
    categories = Category.objects.filter(is_active=True).prefetch_related('products')
    products = Product.objects.filter(is_available=True).select_related('category')

    # Search
    search_query = request.GET.get('q', '')
    if search_query:
        products = products.filter(
            Q(name__icontains=search_query) |
            Q(description__icontains=search_query) |
            Q(category__name__icontains=search_query)
        )

    # Filter by category
    category_slug = request.GET.get('category', '')
    active_category = None
    if category_slug:
        active_category = get_object_or_404(Category, slug=category_slug)
        products = products.filter(category=active_category)

    context = {
        'categories': categories,
        'products': products,
        'search_query': search_query,
        'active_category': active_category,
    }
    return render(request, 'menu/index.html', context)


def product_detail(request, slug):
    product = get_object_or_404(Product, slug=slug, is_available=True)
    related = Product.objects.filter(
        category=product.category, is_available=True
    ).exclude(pk=product.pk)[:4]
    return render(request, 'menu/product_detail.html', {
        'product': product, 'related': related
    })


# --- Admin Menu Management ---

@login_required
@admin_required
def product_list(request):
    products = Product.objects.select_related('category').order_by('-created_at')
    categories = Category.objects.all()
    category_filter = request.GET.get('category', '')
    if category_filter:
        products = products.filter(category__slug=category_filter)
    search = request.GET.get('q', '')
    if search:
        products = products.filter(name__icontains=search)
    return render(request, 'menu/product_list.html', {
        'products': products,
        'categories': categories,
        'category_filter': category_filter,
        'search': search
    })


@login_required
@admin_required
def product_create(request):
    if request.method == 'POST':
        form = ProductForm(request.POST, request.FILES)
        if form.is_valid():
            form.save()
            messages.success(request, 'Product created successfully!')
            return redirect('menu:product_list')
    else:
        form = ProductForm()
    return render(request, 'menu/product_form.html', {'form': form, 'title': 'Add Product'})


@login_required
@admin_required
def product_edit(request, pk):
    product = get_object_or_404(Product, pk=pk)
    if request.method == 'POST':
        form = ProductForm(request.POST, request.FILES, instance=product)
        if form.is_valid():
            form.save()
            messages.success(request, 'Product updated successfully!')
            return redirect('menu:product_list')
    else:
        form = ProductForm(instance=product)
    return render(request, 'menu/product_form.html', {'form': form, 'title': 'Edit Product', 'product': product})


@login_required
@admin_required
def product_delete(request, pk):
    product = get_object_or_404(Product, pk=pk)
    if request.method == 'POST':
        product.delete()
        messages.success(request, 'Product deleted successfully!')
        return JsonResponse({'success': True})
    return JsonResponse({'success': False})


@login_required
@admin_required
def product_toggle(request, pk):
    product = get_object_or_404(Product, pk=pk)
    product.is_available = not product.is_available
    product.save()
    return JsonResponse({'success': True, 'is_available': product.is_available})


@login_required
@admin_required
def category_list(request):
    categories = Category.objects.annotate_product_count() if hasattr(Category.objects, 'annotate_product_count') else Category.objects.all()
    from django.db.models import Count
    categories = Category.objects.annotate(product_count=Count('products')).order_by('order')
    return render(request, 'menu/category_list.html', {'categories': categories})


@login_required
@admin_required
def category_create(request):
    if request.method == 'POST':
        form = CategoryForm(request.POST, request.FILES)
        if form.is_valid():
            form.save()
            messages.success(request, 'Category created!')
            return redirect('menu:category_list')
    else:
        form = CategoryForm()
    return render(request, 'menu/category_form.html', {'form': form, 'title': 'Add Category'})


@login_required
@admin_required
def category_edit(request, pk):
    category = get_object_or_404(Category, pk=pk)
    if request.method == 'POST':
        form = CategoryForm(request.POST, request.FILES, instance=category)
        if form.is_valid():
            form.save()
            messages.success(request, 'Category updated!')
            return redirect('menu:category_list')
    else:
        form = CategoryForm(instance=category)
    return render(request, 'menu/category_form.html', {'form': form, 'title': 'Edit Category'})


@login_required
@admin_required
def category_delete(request, pk):
    category = get_object_or_404(Category, pk=pk)
    if request.method == 'POST':
        category.delete()
        messages.success(request, 'Category deleted!')
        return JsonResponse({'success': True})
    return JsonResponse({'success': False})


def get_product_price(request, pk):
    """AJAX endpoint to get product price by size"""
    product = get_object_or_404(Product, pk=pk)
    size = request.GET.get('size', 'none')
    price = float(product.get_price_for_size(size))
    return JsonResponse({'price': price, 'formatted': f'₱{price:.2f}'})
